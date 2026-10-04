"""Read projections from committed local facts; none of these reads fetch a source."""

from sqlalchemy import func, select

from iirp.business_models import Issuer, MarketQuote, Owner, Security
from iirp.config import settings
from iirp.db import session
from iirp.market_data import MARKETS, source_contract
from iirp.models import ACTIVE, Job
from iirp.providers import sec_configured
from iirp.queue import worker_view


def home():
    with session() as s:
        quotes = {q.symbol: q for q in s.scalars(select(MarketQuote))}
        market = []
        for symbol, name in MARKETS.items():
            row = quotes.get(symbol)
            if row:
                market.append({**row.data, "name": name, "source": row.data.get("source", "Yahoo Finance / yfinance"), "fetched_at": row.fetched_at.isoformat()})
            else:
                failure = s.scalar(
                    select(Job)
                    .where(
                        Job.kind.in_(("market_identity", "market_quote")),
                        Job.target["symbol"].astext == symbol,
                    )
                    .order_by(Job.created_at.desc())
                    .limit(1)
                )
                market.append(
                    {
                        "symbol": symbol,
                        "name": name,
                        "value": None,
                        "change_percent": None,
                        "as_of": None,
                        "status": failure.status if failure else "NOT_FETCHED",
                        "reason": failure.error if failure else "尚未获取；点击更新市场行情",
                    }
                )
        return {
            "data_status": "AVAILABLE" if quotes else "NOT_FETCHED",
            "market": market,
            "worker": worker_view(s),
            "active_jobs": s.scalar(
                select(func.count()).select_from(Job).where(Job.status.in_(ACTIVE))
            ),
            "mode": settings().mode,
            "notice": "日线与申报按来源逐项保存；历史范围和缺口见数据页。",
        }


def feed(session_id="", cursor="", kind="all", order="transaction"):
    from iirp.feed_updates import pending_feed_metadata
    from iirp.sec_facts import feed as read_feed

    with session() as s, s.begin():
        result = read_feed(s, session_id, cursor, kind, order)
        # Polling has its own scalar endpoint; ordinary page reads do not repeat
        # the full latest-revision scan just to report an update count.
        result["new_count"] = 0
        result["pending_summary"], result["pending_filings"] = pending_feed_metadata(s, preview=True)
        return result


def search(q):
    q = q.strip()
    if not q:
        return {
            "items": [],
            "data": {"message": "输入 ticker、公司名称、人员名称或 CIK，搜索本地已保存数据。"},
        }
    if len(q) > 100:
        raise ValueError("搜索关键词最多100个字符")
    term = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with session() as s:
        items = []
        for security in s.scalars(
            select(Security)
            .where(Security.symbol.ilike(term) | Security.name.ilike(term))
            .limit(20)
        ):
            items.append(
                {
                    "id": security.issuer_id or security.id,
                    "kind": "company" if security.issuer_id else "security",
                    "name": security.name or security.symbol,
                    "ticker": security.symbol,
                    "security_id": security.id,
                    "issuer_id": security.issuer_id,
                    "href": "/companies/" + security.issuer_id
                    if security.issuer_id
                    else "/analysis/monthly?tickers=" + security.symbol,
                }
            )
        for model, kind in ((Issuer, "company"), (Owner, "person")):
            for entity in s.scalars(
                select(model).where(model.name.ilike(term) | model.id.ilike(term)).limit(20)
            ):
                if any(x["kind"] == kind and x["id"] == entity.id for x in items):
                    continue
                items.append(
                    {
                        "id": entity.id,
                        "kind": kind,
                        "name": entity.name,
                        "href": ("/companies/" if kind == "company" else "/people/") + entity.id,
                    }
                )
        return {
            "items": items,
            "data": {"message": "仅查询本地证券和主体；获取历史通过详情页明确提交。"},
        }


def providers():
    contract = source_contract()
    return {
        "items": [
            {
                "id": "sec",
                "name": "SEC 官方",
                "configured": sec_configured(),
                "status": "CONFIGURED" if sec_configured() else "NEEDS_CONFIG",
                "message": "六类申报、原文、索引对账与财报附件。"
                if sec_configured()
                else "请在本机配置真实联系邮箱。",
                "budget": "共享 2 请求/秒、最多 2 个执行单元；历史按缺口续作",
            },
            {
                "id": "yfinance",
                "name": "Yahoo 行情与财报候选",
                "configured": True,
                "status": "AVAILABLE" if contract["verified"] else "UNVERIFIED",
                "message": "仅拆股 OHLC 合约已通过普通拆股、反向拆股、股息样例；每次入库仍核对日期和价格版本。"
                if contract["verified"]
                else "当前依赖版本与已验证口径不一致，需要重新验证。",
                "budget": "串行下载；默认 8 个完整历史年，可修改；财报候选需要公告核对",
            },
        ],
        "data": {},
    }
