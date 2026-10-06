"""Local market reads: price coverage, range previews and one symbol's detail."""

from datetime import date

from sqlalchemy import select

from iirp.db import session
from iirp.jobs.batches import ET, resolve_defaults
from iirp.market.cache import cache_facts, coverage_for, price_bars
from iirp.models import (
    Security,
    now,
)


def get_coverage(ticker="", security_id="", start_date="", end_date=""):
    with session() as s:
        from iirp.analysis.history_range import price_range
        if bool(start_date) != bool(end_date):
            raise ValueError("起止日期需要一起提供")
        target = price_range(resolve_defaults(s, {"start_date": start_date, "end_date": end_date}), now())
        query = select(Security)
        if ticker:
            query = query.where(Security.symbol == ticker.upper())
        if security_id:
            query = query.where(Security.id == security_id)
        items = []
        for sec in s.scalars(query.order_by(Security.symbol).limit(100)):
            start = date.fromisoformat(target["target_start_date"])
            end = min(date.fromisoformat(target["target_end_date"]), date.fromisoformat(target["completed_through"]))
            items.append(
                {
                    "id": sec.id,
                    "security_id": sec.id,
                    "symbol": sec.symbol,
                    "name": sec.name,
                    "coverage": coverage_for(s, sec, start, end),
                    "price_range": target,
                }
            )
        return {"items": items, "data": {}}


def preview_price_range(historical_years=None, start_date=None, end_date=None, analysis=None):
    """Local planning only: no job, subscription, identity, or result mutation."""
    from iirp.analysis.calendar import last_completed_session
    from iirp.analysis.history_range import price_range
    from iirp.analysis.research import _interval_rules, plan_scope
    with session() as s:
        params = resolve_defaults(s, {"historical_years": historical_years,
                                      "start_date": start_date, "end_date": end_date})
        stamp = now()
        bounds = None
        if analysis:
            effective = {k: v for k, v in analysis.items() if v is not None}
            if effective["kind"] == "monthly":
                effective.setdefault("current_year", stamp.astimezone(ET).year)
            else:
                effective["current_year"] = _interval_rules(effective, stamp.astimezone(ET).date())[3]
            params["historical_years"] = effective["historical_years"]
            params["analysis_params"] = effective
            bounds = plan_scope(effective, today=last_completed_session(as_of=stamp))
        return price_range(params, stamp, collection=bounds)


def market_detail(symbol):
    from iirp.models import MarketQuote

    with session() as s:
        quote = s.get(MarketQuote, symbol)
        security = s.scalar(
            select(Security).where(Security.symbol == symbol).order_by(Security.id).limit(1)
        )
        bars, cache = price_bars(s, security.id) if security else ([], None)
        use_dataset = cache is not None
        records = (
            bars[-60:] if use_dataset else quote.data.get("records", [])[-60:] if quote else []
        )
        data = (
            {**quote.data, "source": quote.data.get("source", "Yahoo Finance / yfinance")}
            if quote
            else {"symbol": symbol, "status": "NOT_FETCHED"}
        )
        if records:
            data.update(
                chart_start=records[0]["date"],
                chart_end=records[-1]["date"],
                chart_source=cache.provider if use_dataset else data.get("source"),
                chart_basis="仅拆股调整，不含分红再投资" if use_dataset else "供应商日线原始口径",
                chart_intraday=False,
                chart_note="日线序列，当日数据可能尚未收盘；不是分时走势" if not use_dataset else "研究日线（24 小时缓存）",
                **(cache_facts(cache) if use_dataset else {}),
            )
        return {
            "items": records,
            "data": data,
        }
