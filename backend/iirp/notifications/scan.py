"""Notifications made from new local data: followed people's trades and rule matches.

The worker calls ``scan`` every half minute; only one scan runs at a time (advisory
lock), and every notification has a unique dedupe key, so reading the same filing or
report again (a re-parse, a restart, an overlapping scan) adds nothing.

Followed people. An SEC filing is notified once (``trade:sec:<accession>``) when it has
current open-market purchases or sales of a followed filer and the SEC accepted it after
notifications were turned on and after that filer was added. A House or OGE report is
notified once (``trade:political:<report>``) when all its pages are read, it has at least
one purchase or sale, it is not superseded, and it was found after notifications were
turned on and after the person was added; its report date may be at most a week before
notifications were turned on, so a first collection of old reports adds nothing. A Form
4/A or a linked amended report is notified as an amendment and never counts toward a rule.

Rules look at SEC open-market purchases (P) only, accepted in the last three days and
after notifications were turned on, in the S&P 500, the Nasdaq-100 or the watchlist
(or every company when chosen), plan trades left out by default:
- a CEO or CFO bought at least the threshold in one filing (``rule_executive:<accession>``),
  amounts priced and checked as on the Insider overview (``insider/overview.py``);
- at least N people bought the same company within D days, counted exactly as the
  overview's cluster list (``overview.clusters``), at most once per company per D days
  (``rule_cluster:<issuer>:<first trade day>``).

Only public facts are stored: who, which stock, buy or sell, amounts, trade and
publication dates and the public link of the original filing or report.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Text, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, insert

from iirp.analysis.calendar import ET
from iirp.insider.overview import _priced, _rows, clusters, ticker_tokens
from iirp.insider.prices import display_ticker, money_text, overview_settings
from iirp.models import (
    Filing,
    IndexConstituent,
    Issuer,
    NotablePerson,
    Owner,
    TransactionEvent,
    WatchlistItem,
    now,
)
from iirp.models.base import uid
from iirp.models.notifications import Notification
from iirp.models.political import PoliticalReport, PoliticalTransaction
from iirp.notable import people as notable
from iirp.notable.trades import asset_kind, oge_role, sec_roles
from iirp.notifications import settings as notification_settings
from iirp.political.reads import active_reports

LOOKBACK = timedelta(days=30)
RULE_LOOKBACK = timedelta(days=3)
REPORT_SLACK = timedelta(days=7)
# Trades filed late still count when the trade was at most this long before the scan.
RULE_TRADE_DAYS = 90
TRADE_TYPES = {"purchase": "buy", "sale": "sell", "partial_sale": "sell"}


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return money_text(value)
    return value


def _add(s, key, kind, data, *, at, push, person_key=None, issuer_id=None):
    row = s.execute(insert(Notification).values(
        id=uid(), dedupe_key=key, kind=kind, person_key=person_key, issuer_id=issuer_id, created_at=at,
        data=_plain(data), push_state=push,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(Notification.id)).first()
    return 1 if row else 0


def _sum(values):
    values = [value for value in values if value is not None]
    return sum(values, Decimal(0)) if values else None


def _plan(rows):
    """Whether a filing's sales were under a 10b5-1 plan: yes, no, partly or unknown."""
    flags = [True if row.is_plan else (False if (row.plan_flag or "").strip().lower() in ("0", "false") else None)
             for row in rows]
    if all(flag is True for flag in flags):
        return "yes"
    if all(flag is False for flag in flags):
        return "no"
    return "partly" if any(flag is True for flag in flags) else "unknown"


def _sec_trades(s, ciks, names, *, floor, at, push):
    if not ciks:
        return 0
    event = TransactionEvent
    rows = s.execute(
        select(event.id, event.accession, event.owner_ids, event.transaction_code, event.transaction_date,
               event.reported_amount, event.is_plan, event.data["raw_10b5_1_flag"].astext.label("plan_flag"),
               event.data["owner_relationships"].label("relationships"),
               event.data["owner_names"].label("owner_names"), event.data["currency"].astext.label("currency"),
               event.filing_ticker, Filing.accepted_at, Filing.form, Filing.index_url,
               Issuer.id.label("issuer_id"), Issuer.name.label("issuer_name"), Issuer.ticker.label("issuer_ticker"))
        .join(Filing, Filing.accession == event.accession).join(Issuer, Issuer.id == event.issuer_id)
        .where(event.status == "CURRENT", event.transaction_code.in_(("P", "S")), event.data["table"].astext == "I",
               Filing.visible.is_(True), Filing.accepted_at >= floor,
               event.owner_ids.has_any(cast(sorted(ciks), ARRAY(Text))))
        .order_by(Filing.accepted_at, event.id)).all()
    filings = defaultdict(list)
    for row in rows:
        filings[row.accession].append(row)
    owner_names = {}
    wanted = {owner for row in rows for owner in row.owner_ids if owner in ciks}
    if wanted:
        owner_names = dict(s.execute(select(Owner.id, Owner.name).where(Owner.id.in_(wanted))).all())
    created = 0
    for accession, items in filings.items():
        first = items[0]
        # The filer followed longest, among those followed before the SEC accepted the filing.
        eligible = sorted({owner for owner in first.owner_ids if owner in ciks and first.accepted_at >= ciks[owner]},
                          key=lambda owner: (ciks[owner], owner))
        if not eligible:
            continue
        owner = eligible[0]
        key = f"sec:{owner}"
        roles, title = sec_roles((first.relationships or {}).get(owner))
        name = (names.get(key) or {}).get("name") or (first.owner_names or {}).get(owner) or owner_names.get(owner) or owner
        buys = [row for row in items if row.transaction_code == "P"]
        sells = [row for row in items if row.transaction_code == "S"]
        days = sorted(row.transaction_date for row in items if row.transaction_date)
        data = {
            "source": "sec", "person": {"key": key, "name": name, "roles": roles, "title": title},
            "ticker": display_ticker(first.issuer_ticker, first.filing_ticker), "asset_name": first.issuer_name,
            "issuer_id": first.issuer_id, "currency": first.currency,
            "buy": {"rows": len(buys), "amount": _sum(row.reported_amount for row in buys)} if buys else None,
            "sell": {"rows": len(sells), "amount": _sum(row.reported_amount for row in sells), "plan": _plan(sells)}
            if sells else None,
            "traded_from": days[0] if days else None, "traded_to": days[-1] if days else None,
            "published": first.accepted_at.astimezone(ET).date(), "published_at": first.accepted_at,
            "url": first.index_url, "amendment": (first.form or "").upper().endswith("/A"),
            "trade_id": first.id, "rows": len(items),
        }
        created += _add(s, f"trade:sec:{accession}", "trade", data, at=at, push=push, person_key=key,
                        issuer_id=first.issuer_id)
    return created


def _political_trades(s, followed, names, *, floor, start, at, push):
    keys = {key for key in followed if not key.startswith("sec:")}
    if not keys:
        return 0
    people = {person.id: (key, person) for key, rows in notable.political_people(s, keys).items() for person in rows}
    if not people:
        return 0
    published = func.coalesce(PoliticalReport.report_date, PoliticalReport.filed_date)
    reports = s.scalars(select(PoliticalReport).where(
        PoliticalReport.person_id.in_(sorted(people)), *active_reports(),
        PoliticalReport.status.in_(("parsed", "review")), PoliticalReport.page_count > 0,
        PoliticalReport.pages_done >= PoliticalReport.page_count, PoliticalReport.discovered_at >= floor,
        published >= (start - REPORT_SLACK).astimezone(ET).date(),
    ).order_by(PoliticalReport.discovered_at, PoliticalReport.id)).all()
    created = 0
    for report in reports:
        key, person = people[report.person_id]
        if report.discovered_at < followed[key]:
            continue
        rows = s.scalars(select(PoliticalTransaction).where(PoliticalTransaction.report_id == report.id)
                         .order_by(PoliticalTransaction.page, PoliticalTransaction.row_number)).all()
        trades = [row for row in rows if row.transaction_type in TRADE_TYPES]
        if not trades:
            continue
        day = report.report_date or report.filed_date
        days = sorted(row.transaction_date for row in trades if row.transaction_date)
        largest = sorted(trades, key=lambda row: (-(row.amount_low or 0), row.page, row.row_number))[:3]
        tickers = {row.ticker for row in trades}
        data = {
            "source": report.source,
            "person": {"key": key, "name": (names.get(key) or {}).get("name") or person.name,
                       "roles": ["house"] if report.source == "house" else [oge_role(person.position)],
                       "title": person.position if report.source == "oge" else None, "district": person.district},
            "ticker": trades[0].ticker if len(tickers) == 1 else None,
            "asset_name": trades[0].asset_name if len(trades) == 1 else None,
            "buys": sum(TRADE_TYPES[row.transaction_type] == "buy" for row in trades),
            "sells": sum(TRADE_TYPES[row.transaction_type] == "sell" for row in trades),
            "items": [{"ticker": row.ticker, "asset_name": row.asset_name, "side": TRADE_TYPES[row.transaction_type],
                       "asset_kind": asset_kind(row.asset_type), "amount_low": row.amount_low,
                       "amount_high": row.amount_high} for row in largest],
            "traded_from": days[0] if days else None, "traded_to": days[-1] if days else None,
            "published": day, "url": report.url, "amendment": report.amendment_status == "linked",
            "trade_id": trades[0].id if len(trades) == 1 else None, "rows": len(trades),
        }
        created += _add(s, f"trade:political:{report.id}", "trade", data, at=at, push=push, person_key=key)
    return created


def _scope(s):
    """The companies rules look at by default: S&P 500 and Nasdaq-100 members (by CIK or code)
    and the watchlist's codes."""
    ciks, tickers = set(), set()
    for cik, ticker in s.execute(select(IndexConstituent.cik, IndexConstituent.ticker)
                                 .where(IndexConstituent.index_name.in_(("sp500", "nasdaq100")))):
        if cik:
            ciks.add(cik)
        tickers |= ticker_tokens(ticker)
    for ticker in s.scalars(select(WatchlistItem.ticker)):
        tickers |= ticker_tokens(ticker)
    return ciks, tickers


def _rules(s, rules, *, since, at, push):
    event = TransactionEvent
    new = s.execute(
        select(event.id, event.issuer_id, event.accession, event.version_id, event.accepted_at,
               event.filing_ticker, Issuer.ticker.label("issuer_ticker"))
        .join(Filing, Filing.accession == event.accession).join(Issuer, Issuer.id == event.issuer_id)
        .where(event.accepted_at >= since, event.status == "CURRENT", event.transaction_code == "P",
               event.data["table"].astext == "I", Filing.visible.is_(True),
               ~func.upper(func.coalesce(Filing.form, "")).like("%/A"))).all()
    if rules["scope"] != "all":
        ciks, tickers = _scope(s)
        new = [row for row in new if row.issuer_id in ciks
               or ticker_tokens(row.issuer_ticker, row.filing_ticker) & tickers]
    codes = set().union(*(ticker_tokens(row.issuer_ticker, row.filing_ticker) for row in new)) if new else set()
    if not codes:
        return 0
    fresh = {row.id: row for row in new}
    issuers = {row.issuer_id for row in new}
    rows = [row for row in _rows(s, days=RULE_TRADE_DAYS, exclude_plans=rules["exclude_plans"], at=at, tickers=codes)
            if row["transaction_code"] == "P" and row["issuer_id"] in issuers]
    created = 0
    # A CEO or CFO bought at least the threshold in one filing.
    executive = [row for row in rows if row["id"] in fresh and (row["is_ceo"] or row["is_cfo"])]
    if executive:
        _priced(s, executive, overview_settings())
        by_filing = defaultdict(list)
        for row in executive:
            by_filing[fresh[row["id"]].accession].append(row)
        for accession, items in by_filing.items():
            amount = _sum(row["counted"] for row in items)
            if amount is None or amount < rules["executive_min"]:
                continue
            first = items[0]
            owner = next(iter(first["owner_ids"]), None)
            owner_row = s.get(Owner, owner) if owner else None
            accepted = fresh[first["id"]].accepted_at
            data = {
                "source": "sec", "ticker": first["ticker"], "asset_name": first["name"], "issuer_id": first["issuer_id"],
                "person": {"key": f"sec:{owner}" if owner else None, "name": owner_row.name if owner_row else owner,
                           "roles": [role for role, flag in (("ceo", first["is_ceo"]), ("cfo", first["is_cfo"])) if flag]},
                "amount": amount, "rows": len(items), "threshold": rules["executive_min"],
                "traded_from": min(row["transaction_date"] for row in items),
                "traded_to": max(row["transaction_date"] for row in items),
                "published": accepted.astimezone(ET).date(), "published_at": accepted,
                "url": s.scalar(select(Filing.index_url).where(Filing.accession == accession)),
                "trade_id": first["id"],
            }
            created += _add(s, f"rule_executive:{accession}", "rule_executive", data, at=at, push=push,
                            issuer_id=first["issuer_id"])
    # At least N people bought the same company within D days (the overview's cluster rule).
    windows = clusters(rows, days=rules["cluster_days"], people=rules["cluster_people"])
    recent = at - timedelta(days=rules["cluster_days"])
    for window in windows:
        if not any(row["id"] in fresh for row in window):
            continue
        issuer = window[0]["issuer_id"]
        if s.scalar(select(Notification.id).where(Notification.kind == "rule_cluster", Notification.issuer_id == issuer,
                                                  Notification.created_at >= recent).limit(1)):
            continue
        _priced(s, window, overview_settings())
        owners = sorted({owner for row in window for owner in row["owner_ids"]})
        named = dict(s.execute(select(Owner.id, Owner.name).where(Owner.id.in_(owners))).all())
        accepted = max(fresh[row["id"]].accepted_at for row in window if row["id"] in fresh)
        first_day = min(row["transaction_date"] for row in window)
        data = {
            "source": "sec", "ticker": window[0]["ticker"], "asset_name": window[0]["name"], "issuer_id": issuer,
            "people": len(owners), "names": [named.get(owner, owner) for owner in owners][:3], "rows": len(window),
            "amount": _sum(row["counted"] for row in window), "days": rules["cluster_days"],
            "traded_from": first_day, "traded_to": max(row["transaction_date"] for row in window),
            "published": accepted.astimezone(ET).date(), "published_at": accepted,
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={issuer}&type=4&owner=include&count=40",
        }
        created += _add(s, f"rule_cluster:{issuer}:{first_day.isoformat()}", "rule_cluster", data, at=at, push=push,
                        issuer_id=issuer)
    return created


def scan(s, at=None):
    """Add the notifications that new local data calls for; returns how many were added."""
    at = at or now()
    settings = notification_settings.load(s)
    start = notification_settings.enabled_at(settings)
    if start is None:
        return 0
    if not s.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext("iirp.notifications.scan")))):
        return 0
    push = "pending" if settings["ntfy"]["enabled"] and settings["ntfy"]["topic"] else None
    followed = dict(s.execute(select(NotablePerson.key, NotablePerson.added_at)).all())
    names = notable.default_map()
    floor = max(start, at - LOOKBACK)
    ciks = {key[4:]: max(added, start) for key, added in followed.items() if key.startswith("sec:")}
    created = _sec_trades(s, ciks, names, floor=floor, at=at, push=push)
    created += _political_trades(s, followed, names, floor=floor, start=start, at=at, push=push)
    if settings["rules"]["enabled"]:
        created += _rules(s, settings["rules"], since=max(start, at - RULE_LOOKBACK), at=at, push=push)
    return created


def tick():
    """One scan and, when messages wait, the push job; called by the worker every half minute."""
    import logging

    from sqlalchemy.exc import SQLAlchemyError

    from iirp.db import session
    from iirp.notifications import push

    try:
        with session() as s, s.begin():
            scan(s)
    except SQLAlchemyError:
        logging.exception("notification scan failed")
    push.tick()
