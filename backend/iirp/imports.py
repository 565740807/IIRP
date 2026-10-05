"""Auditable CSV preview followed by a durable, fenced local import."""

import csv
import io
import re
from datetime import date, datetime
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from iirp.analytics.calendar import last_completed_session, sessions
from iirp.business_models import (
    CoverageSegment,
    DatasetBar,
    EarningsEvent,
    ImportPreview,
    MarketBar,
    PriceDataset,
    Security,
)
from iirp.db import session
from iirp.market_data import digest, latest_dataset, number, price_bars, validate_bar
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
    reader = csv.DictReader(io.StringIO(values["csv"].lstrip("\ufeff")))
    required = (
        {"date", "open", "high", "low", "close"}
        if values["kind"] == "market"
        else {"announced_date", "fiscal_year", "fiscal_quarter", "time_precision"}
    )
    allowed = required | (
        {"volume", "adj_close"} if values["kind"] == "market" else {"announced_at", "source_url"}
    )
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
            day = date.fromisoformat(
                row["date" if values["kind"] == "market" else "announced_date"].strip()
            )
            if day in seen:
                raise ValueError("重复日期；请先合并或修正重复行")
            seen.add(day)
            if values["kind"] == "market":
                if day not in sessions(day, day):
                    raise ValueError("不是美股交易日")
                output = {"date": str(day)}
                for key in ("open", "high", "low", "close", "volume", "adj_close"):
                    value = number(row.get(key))
                    if row.get(key) and value is None:
                        raise ValueError(key + " 不是有限数值")
                    output[key] = str(value) if value is not None else None
                _, status, reason = validate_bar(
                    output, SimpleNamespace(instrument="EQUITY"), last_completed_session()
                )
                if status != "VALID":
                    raise ValueError(reason)
            else:
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
    return symbol, sorted(records, key=lambda r: r.get("date") or r["announced_date"]), errors


def publication_reason(records, price_basis, old_dates):
    if price_basis != "SPLIT_ONLY":
        return "价格口径未确认，CSV 仅保留为来源观察"
    provided = {row["date"] for row in records}
    missing = sorted(set(old_dates) - provided)
    if missing:
        return f"不同来源不能拼接调整基准；请导入包含既有全部历史的同口径 CSV，当前缺少 {len(missing)} 个已存交易日（从 {missing[0]} 起）"
    return None


def preview(values):
    symbol, records, errors = parse(values)
    source = save_object(values["csv"].encode(), "text/csv")
    with session() as s, s.begin():
        from iirp.storage import register_object

        register_object(s, source)
        security = s.scalar(select(Security).where(Security.symbol == symbol))
        bars, dataset = (
            price_bars(s, security.id)
            if security and values["kind"] == "market"
            else ([], None)
        )
        old = {r["date"]: r for r in bars}
        changes = sum(
            any(
                number(r.get(k)) != number(old[r["date"]].get(k))
                for k in ("open", "high", "low", "close")
            )
            for r in records
            if "date" in r and r["date"] in old
        )
        differences = [
            f"记录 {len(records)} 行；已存日期修改 {changes} 行。",
            "只有点击提交才写入业务事实。",
        ]
        if values["kind"] == "earnings":
            differences.append(
                "财报 CSV 只保存候选及原始来源；不会确认日期、财季或实际时刻，"
                "也不会覆盖已核验事实。请在财报事件中逐项核对来源后再纳入研究。"
            )
        publish_reason = (
            publication_reason(records, values["price_basis"], old)
            if values["kind"] == "market"
            else None
        )
        if publish_reason:
            differences.append(
                publish_reason + "；提交仍会保存原始文件与观察，旧合格版本继续可读。"
            )
        data = {
            "symbol": symbol,
            "records": records,
            "errors": errors,
            "differences": differences,
            "source_url": values["source_url"],
            "price_basis": values["price_basis"],
            "base_dataset_id": dataset.id if dataset else None,
            "publication_reason": publish_reason,
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
        dates = [r.get("date") or r["announced_date"] for r in p.data["records"]]
        batch, _ = _create(
            s,
            {
                "request_id": "import:" + p.id,
                "kind": "market_history" if p.kind == "market" else "earnings",
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
    publish_reason = None
    if p.kind == "earnings":
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

    else:
        if security.status != "VERIFIED":
            raise ValueError("证券身份或交易日历尚未确认")
        s.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(digest(security.id)[:15], 16)}
        )
        old = latest_dataset(s, security.id)
        if (old.id if old else None) != p.data["base_dataset_id"]:
            raise ValueError("预览后价格版本已更新；请重新预览差异再提交")
        old_dates = (
            list(s.scalars(select(DatasetBar.session_date).where(DatasetBar.dataset_id == old.id)))
            if old
            else []
        )
        publish_reason = publication_reason(
            p.data["records"], p.data["price_basis"], [str(day) for day in old_dates]
        )
        verified = publish_reason is None
        links = {}
        for row in p.data["records"]:
            values, status, reason = validate_bar(
                row, security, last_completed_session(calendar=security.calendar)
            )
            if status != "VALID":
                raise ValueError(reason)
            if not verified:
                status, reason = "UNCONFIRMED", publish_reason
            key = digest([row, p.source_hash])
            day = date.fromisoformat(row["date"])
            bar = s.scalar(
                select(MarketBar).where(
                    MarketBar.security_id == security.id,
                    MarketBar.provider == "manual_csv",
                    MarketBar.session_date == day,
                    MarketBar.record_hash == key,
                )
            )
            if bar is None:
                bar = MarketBar(
                    security_id=security.id,
                    session_date=day,
                    provider="manual_csv",
                    source_hash=p.source_hash,
                    record_hash=key,
                    status=status,
                    reason=reason,
                    **values,
                )
                s.add(bar)
                s.flush()
            if verified:
                links[day] = bar.id
        if verified:
            ds = PriceDataset(
                security_id=security.id,
                basis="SPLIT_ONLY",
                basis_key=digest(["manual_csv", p.source_hash]),
                status="PUBLISHED",
                manifest={
                    "provider": "manual_csv",
                    "source_hash": p.source_hash,
                    "parent_dataset": old.id if old else None,
                    "basis_attestation": p.data["source_url"],
                    "records": len(links),
                },
                published_at=now(),
            )
            s.add(ds)
            s.flush()
            s.add_all(
                [
                    DatasetBar(dataset_id=ds.id, session_date=day, bar_id=bar_id)
                    for day, bar_id in links.items()
                ]
            )
        s.add(
            CoverageSegment(
                security_id=security.id,
                provider="manual_csv",
                kind="daily",
                start_date=date.fromisoformat(p.data["records"][0]["date"]),
                end_date=date.fromisoformat(p.data["records"][-1]["date"]),
                status="OBSERVED" if verified else "UNVERIFIED",
                details={"source_hash": p.source_hash, "import_id": p.id, "reason": publish_reason},
                source_hash=p.source_hash,
            )
        )
    p.status = "COMMITTED"
    return {
        "message": "财报候选与来源已保存；请完成独立事实核对"
        if p.kind == "earnings"
        else "CSV 已提交；实际完整性以逐交易日覆盖为准",
        "reason": publish_reason,
        "import_id": p.id,
        "rows": len(p.data["records"]),
        "source_hash": p.source_hash,
    }
