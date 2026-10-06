"""Auditable earnings CSV preview followed by a durable, fenced local import.

Prices are a 24-hour cache fetched from the provider (D14); CSV price imports
are not accepted.
"""

import csv
import io
import re
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select

from iirp.business_models import EarningsEvent, ImportPreview, Security
from iirp.db import session
from iirp.models import now
from iirp.storage import save_object


def view(preview):
    d = preview.data
    return {
        "id": preview.id,
        "kind": preview.kind,
        "status": preview.status,
        "valid_rows": len(d["records"]),
        "errors": d["errors"],
        "preview": d["records"][:20],
        "differences": d["differences"],
        "batch_id": preview.batch_id,
    }


def parse(values):
    symbol = values["ticker"].strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", symbol):
        raise ValueError("CSV 证券代码格式不合法")
    source = urlsplit(values["source_url"])
    if source.scheme not in ("http", "https") or not source.hostname:
        raise ValueError("请填写可追溯的 HTTP(S) 来源地址")
    if values["kind"] != "earnings":
        raise ValueError("只支持导入财报 CSV；行情为 24 小时缓存，按需从来源获取")
    reader = csv.DictReader(io.StringIO(values["csv"].lstrip("\ufeff")))
    required = {"announced_date", "fiscal_year", "fiscal_quarter", "time_precision"}
    allowed = required | {"announced_at", "source_url"}
    if (
        not reader.fieldnames
        or len(set(reader.fieldnames)) != len(reader.fieldnames)
        or not required.issubset(reader.fieldnames)
        or set(reader.fieldnames) - allowed
    ):
        raise ValueError(
            "CSV 表头应包含 "
            + ", ".join(sorted(required))
            + "；可选 "
            + ", ".join(sorted(allowed - required))
        )
    records = []
    errors = []
    seen = set()
    for index, row in enumerate(reader, 2):
        try:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("列数不一致")
            day = date.fromisoformat(row["announced_date"].strip())
            if day in seen:
                raise ValueError("重复日期；请先合并或修正重复行")
            seen.add(day)
            output = {
                "announced_date": str(day),
                "fiscal_year": int(row["fiscal_year"]),
                "fiscal_quarter": int(row["fiscal_quarter"]),
                "time_precision": row["time_precision"].strip(),
                "announced_at": row.get("announced_at") or None,
                "source_url": row.get("source_url") or values["source_url"],
            }
            if not 1 <= output["fiscal_year"] <= 9998 or output["fiscal_quarter"] not in (
                1,
                2,
                3,
                4,
            ):
                raise ValueError("财年或财季不合法")
            if output["time_precision"] not in (
                "exact",
                "before_open",
                "after_close",
                "date_only",
                "intraday",
                "conflict",
            ):
                raise ValueError("公告时间精度不合法")
            url = urlsplit(output["source_url"])
            if url.scheme not in ("https", "http") or not url.hostname:
                raise ValueError("公告证据地址不合法")
            if output["announced_at"]:
                stamp = datetime.fromisoformat(output["announced_at"].replace("Z", "+00:00"))
                if (
                    stamp.tzinfo is None
                    or stamp.astimezone(ZoneInfo("America/New_York")).date() != day
                ):
                    raise ValueError("公告时间需要时区并与美东日期一致")
                output["announced_at"] = stamp.isoformat()
            if output["time_precision"] == "exact" and not output["announced_at"]:
                raise ValueError("精确公告需要完整时间与时区")
            records.append(output)
        except (ValueError, TypeError) as exc:
            errors.append(f"第 {index} 行：{exc}")
    if not records:
        errors.append("没有可导入记录")
    if len(errors) > 100:
        errors = errors[:100] + ["其余错误省略；请修正全部 CSV 后重新预览"]
    return symbol, sorted(records, key=lambda r: r["announced_date"]), errors


def preview(values):
    symbol, records, errors = parse(values)
    source = save_object(values["csv"].encode(), "text/csv")
    with session() as s, s.begin():
        from iirp.storage import register_object

        register_object(s, source)
        differences = [
            f"记录 {len(records)} 行。",
            "只有点击提交才写入业务事实。",
            "财报 CSV 只保存候选及原始来源；不会确认日期、财季或实际时刻，"
            "也不会覆盖已核验事实。请在财报事件中逐项核对来源后再纳入研究。",
        ]
        data = {
            "symbol": symbol,
            "records": records,
            "errors": errors,
            "differences": differences,
            "source_url": values["source_url"],
        }
        p = ImportPreview(
            kind=values["kind"],
            source_hash=source["sha256"],
            data=data,
            status="INVALID" if errors else "PREVIEW",
        )
        s.add(p)
        s.flush()
        return view(p)


def commit(preview_id):
    from iirp.lifecycle import _create

    with session() as s, s.begin():
        p = s.get(ImportPreview, preview_id, with_for_update=True)
        if not p:
            raise LookupError("导入预览不存在")
        if p.batch_id:
            return view(p)
        if p.data["errors"]:
            raise ValueError("先修正 CSV 中全部错误再提交")
        if p.kind != "earnings":
            raise ValueError("行情 CSV 导入已停用；行情为 24 小时缓存，按需从来源获取")
        dates = [r["announced_date"] for r in p.data["records"]]
        batch, _ = _create(
            s,
            {
                "request_id": "import:" + p.id,
                "kind": "earnings",
                "tickers": [p.data["symbol"]],
                "start_date": min(dates),
                "end_date": max(dates),
                "purpose": "CSV 导入",
                "import_id": p.id,
            },
        )
        p.status = "QUEUED"
        p.batch_id = batch.id
        return view(p)


def persist(s, job):
    """Caller holds the job lease fence. Input observations and version are committed together."""
    p = s.get(ImportPreview, job.target["import_id"], with_for_update=True)
    security = s.get(Security, job.target["security_id"])
    if p.status == "COMMITTED":
        return {
            "message": "此导入已经提交",
            "import_id": p.id,
            "source_hash": p.source_hash,
            "reason": p.data.get("publication_reason"),
        }
    if p.kind != "earnings":
        raise ValueError("行情 CSV 导入已停用；行情为 24 小时缓存，按需从来源获取")
    for row in p.data["records"]:
        event = s.scalar(
            select(EarningsEvent).where(
                EarningsEvent.security_id == security.id,
                EarningsEvent.announced_date == date.fromisoformat(row["announced_date"]),
            )
        )
        observed = now().isoformat()
        evidence = {
            "kind": "user_csv",
            "provider": "user_csv",
            "source_hash": p.source_hash,
            "source_url": row["source_url"],
            "candidate": dict(row),
            "verified": False,
            "observed_at": observed,
            "recorded_at": observed,
        }
        if event is None:
            event = EarningsEvent(
                security_id=security.id,
                announced_date=date.fromisoformat(row["announced_date"]),
                fiscal_year=row["fiscal_year"],
                fiscal_quarter=row["fiscal_quarter"],
                announced_at=None,
                time_precision="date_only",
                verified=False,
                is_estimate=date.fromisoformat(row["announced_date"]) > now().astimezone(ZoneInfo("America/New_York")).date(),
                status="CANDIDATE",
                evidence=[],
                revision=0,
            )
            evidence["first_observed_at"] = p.created_at.isoformat()
            s.add(event)
        elif any(
            item.get("provider") == "user_csv"
            and item.get("source_hash") == p.source_hash
            and item.get("candidate") == row
            for item in (event.evidence or [])
        ):
            continue
        # Admission is not source verification. Preserve all existing facts
        # and explicit reviews, including when imported claims contradict.
        event.evidence = [*(event.evidence or []), evidence]
        event.revision += 1
        event.updated_at = now()
    publish_reason = "财报 CSV 候选已保存，日期、财季与实际时刻仍须逐项核对；已有核验事实保持。"
    p.data = {**p.data, "publication_reason": publish_reason}
    p.status = "COMMITTED"
    return {
        "message": "财报候选与来源已保存；请完成独立事实核对",
        "reason": publish_reason,
        "import_id": p.id,
        "rows": len(p.data["records"]),
        "source_hash": p.source_hash,
    }
