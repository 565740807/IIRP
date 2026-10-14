"""Trades of followed people from SEC Form 4 and government disclosures, newest publication first.

Every row is a reported fact; nothing is scored. The three sources share one
order: the publication date (SEC: the US Eastern day the SEC accepted the
filing; House and OGE: the report date), then SEC before House before OGE,
then a fixed identifier, so pages never repeat or skip a row.

SEC rows are open-market purchases (P) and sales (S) in the non-derivative
table, from current (not superseded) transactions of visible filings; their
amount is the filed shares times the filed price. Government rows come from
reports that are not superseded and not unresolved amendments (the same rows
the government disclosure pages count); their amount is the reported range.

A government row read from a scanned page is marked "scanned". "Needs review"
is kept for rows with a stated doubt, each named: the direction is unknown or
was corrected, the code was inferred from the name, the date or the amount is
missing or implausible, option terms are incomplete, or the text is garbled.

Pages read local tables only. Each row names the code its price windows use
(``price_ticker``): the stock's code, or for an option the underlying stock's;
bonds, funds, rows of an unknown kind and rows without a code have none
(``price_none`` says which). The windows themselves come from the Insider
windows (``analysis/insider_windows.py``), anchored on the trade date and on
the publication (``published_basis``: the last close before the market could
react, the same rule as the SEC trade page: a filing accepted after the close
counts from that close, one accepted before or during a session from the close
before; a report known only by its date counts from the close before that
day). A person's summary describes the windows that local prices already allow
and never asks for prices.
"""

import base64
import binascii
import json
import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from statistics import median

from sqlalchemy import Text, case, cast, func, literal, select, tuple_, union_all
from sqlalchemy.dialects.postgresql import ARRAY

from iirp.analysis.calendar import ET, reaction_session
from iirp.analysis.insider_windows import provider_symbol, trade_windows
from iirp.insider.overview import states_any, ticker_tokens
from iirp.insider.prices import display_ticker, money_text, price_text, shares_text
from iirp.messages import NotFoundError, UserError
from iirp.models import Filing, Issuer, Owner, TransactionEvent
from iirp.models.political import PoliticalPerson, PoliticalReport, PoliticalTransaction
from iirp.notable import people as notable
from iirp.political.reads import active_reports

SOURCE_RANK = {"sec": 2, "house": 1, "oge": 0}
DIRECTIONS = {"buy": (("P",), ("purchase",)), "sell": (("S",), ("sale", "partial_sale"))}
NO_DATE = date(1900, 1, 1)
# Stated doubts of a government row, by parser warning.
REASONS = {
    "unknown_transaction_type": "direction_unknown",
    "direction_conflict": "direction_unknown",
    "direction_text_normalized": "direction_corrected",
    "missing_transaction_date": "date_missing",
    "missing_amount_low": "amount_missing",
    "incomplete_option": "option_incomplete",
    "text_encoding": "text_garbled",
}
# The value ranges of STOCK Act and OGE 278-T reports; any other range was misread.
RANGES = {
    (Decimal(low), Decimal(high) if high else None) for low, high in (
        ("1", "1000"), ("1001", "15000"), ("15001", "50000"), ("50001", "100000"),
        ("100001", "250000"), ("250001", "500000"), ("500001", "1000000"),
        ("1000001", "5000000"), ("5000001", "25000000"), ("25000001", "50000000"),
        ("50000001", None), ("1000001", None),
    )
}
# A trade reported more than a year before its report is a likely misread year.
LATE_DAYS = 365
OGE_DEPARTMENTS = (
    ("treasury", "treasury"), ("commerce", "commerce"), ("defense", "defense"),
    ("justice", "justice"), ("interior", "interior"), ("energy", "energy"),
    ("health and human services", "hhs"), ("housing and urban development", "hud"),
    ("homeland security", "homeland_security"), ("education", "education"), ("labor", "labor"),
    ("transportation", "transportation"), ("environmental protection agency", "epa"),
    ("navy", "navy"), ("army", "army"), ("air force", "air_force"), ("state", "state"),
    ("agriculture", "agriculture"), ("veterans affairs", "veterans_affairs"),
)


def oge_role(position):
    """A short role code for an OGE position ("oge.commerce" for the Secretary of Commerce)."""
    value = (position or "").strip().lower()
    if value == "president":
        return "president"
    if value.startswith("vice president"):
        return "vice_president"
    if value.startswith("attorney general"):
        return "attorney_general"
    match = re.match(r"(secretary|administrator)\s*\((.*)\)", value)
    if match:
        for words, code in OGE_DEPARTMENTS:
            if words in match.group(2):
                return f"{match.group(1)}.{code}"
    return "official"


def _truth(value):
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def sec_roles(relationship):
    """Role codes of one filer on one filing (ceo, cfo, officer, director, ten_percent, other)
    and the title as filed."""
    relationship = relationship or {}
    roles, title = [], relationship.get("officer_title") or None
    if _truth(relationship.get("is_officer")):
        text = (title or "").upper()
        if re.search(r"\bCEO\b|CHIEF\s+EXECUTIVE", text):
            roles.append("ceo")
        if re.search(r"\bCFO\b|CHIEF\s+FINANCIAL", text):
            roles.append("cfo")
        if not roles:
            roles.append("officer")
    if _truth(relationship.get("is_director")):
        roles.append("director")
    if _truth(relationship.get("is_ten_percent_owner")):
        roles.append("ten_percent")
    if _truth(relationship.get("is_other")):
        roles.append("other")
        title = title or relationship.get("other_text")
    return roles, title


def asset_kind(asset_type):
    value = (asset_type or "").lower()
    if "bond" in value:
        return "bond"
    return value if value in ("stock", "option", "fund") else "other"


@lru_cache(maxsize=4096)
def _basis_of(value):
    return date.fromisoformat(reaction_session(value)["baseline_date"])


def published_basis(published_at=None, published=None):
    """The close a publication-based window starts from (see the module note)."""
    if published_at is not None:
        return _basis_of(published_at.astimezone(ET).isoformat())
    return _basis_of(published.isoformat()) if published else None


def _price_code(ticker, kind):
    """The code the price windows use: a stock's own, an option's underlying stock (reports
    name the underlying); bonds, funds and rows of an unknown kind have none."""
    if kind not in ("stock", "option"):
        return {"price_ticker": None, "price_none": kind}
    if not provider_symbol(ticker):
        return {"price_ticker": None, "price_none": "no_ticker"}
    return {"price_ticker": ticker.strip().upper(), "price_none": None}


def review_reasons(row, published):
    reasons = [REASONS[warning] for warning in row.warnings or [] if warning in REASONS]
    if row.ticker_origin == "inferred":
        reasons.append("ticker_inferred")
    if row.transaction_date and published:
        if row.transaction_date > published:
            reasons.append("date_after_report")
        elif (published - row.transaction_date).days > LATE_DAYS:
            reasons.append("date_unlikely")
    if row.amount_low is not None and (row.amount_low, row.amount_high) not in RANGES:
        reasons.append("amount_unusual")
    if len(re.findall(r"[A-Za-z]", row.asset_name or "")) < 3:
        reasons.append("asset_unreadable")
    return list(dict.fromkeys(reasons))


def _decode(cursor):
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if (not isinstance(key, list) or len(key) != 3 or not isinstance(key[1], int)
                or not isinstance(key[2], str)):
            raise ValueError
        return date.fromisoformat(key[0]), key[1], key[2]
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise UserError("notable.cursor_invalid") from exc


def _encode(published, rank, tie):
    return base64.urlsafe_b64encode(json.dumps([published.isoformat(), rank, tie]).encode()).decode()


def _sec_query(ciks, *, issuer_id, ticker, codes, start, end):
    event = TransactionEvent
    published = func.date(func.timezone("America/New_York", Filing.accepted_at))
    tie = func.concat(func.to_char(func.timezone("UTC", Filing.accepted_at), "YYYYMMDDHH24MISS"), ":", event.id)
    query = (select(literal("sec").label("source"), published.label("published"), literal(SOURCE_RANK["sec"]).label("rank"),
                    tie.label("tie"), event.id.label("ref"), Filing.accession.label("group_id"))
             .join(Filing, Filing.accession == event.accession)
             .where(event.status == "CURRENT", event.transaction_code.in_(codes),
                    event.data["table"].astext == "I", Filing.visible.is_(True), Filing.accepted_at.is_not(None),
                    event.owner_ids.has_any(cast(sorted(ciks), ARRAY(Text)))))
    if issuer_id:
        query = query.where(event.issuer_id == issuer_id)
    if ticker:
        query = query.join(Issuer, Issuer.id == event.issuer_id).where(
            states_any(func.concat_ws(" ", Issuer.ticker, event.filing_ticker), [ticker]))
    if start:
        query = query.where(published >= start)
    if end:
        query = query.where(published <= end)
    return query


def _political_query(ids, *, tickers, types, source, start, end, assets="all"):
    published = func.coalesce(PoliticalReport.report_date, PoliticalReport.filed_date, literal(NO_DATE))
    rank = case((PoliticalReport.source == "house", SOURCE_RANK["house"]), else_=SOURCE_RANK["oge"])
    # Rows of one report keep their order on the page under the descending sort.
    tie = func.concat(PoliticalReport.id, ":", func.lpad(cast(99999 - PoliticalTransaction.page, Text), 5, "0"), ":",
                      func.lpad(cast(99999 - PoliticalTransaction.row_number, Text), 5, "0"))
    query = (select(PoliticalReport.source.label("source"), published.label("published"), rank.label("rank"),
                    tie.label("tie"), PoliticalTransaction.id.label("ref"), PoliticalReport.id.label("group_id"))
             .join(PoliticalReport, PoliticalTransaction.report_id == PoliticalReport.id)
             .where(*active_reports(), PoliticalReport.person_id.in_(sorted(ids))))
    if assets == "equities":
        query = query.where(PoliticalTransaction.asset_type.in_(("stock", "option")))
    if source:
        query = query.where(PoliticalReport.source == source)
    if tickers is not None:
        query = query.where(func.upper(func.replace(PoliticalTransaction.ticker, ".", "-")).in_(sorted(tickers)))
    if types is not None:
        query = query.where(PoliticalTransaction.transaction_type.in_(types))
    if start:
        query = query.where(published >= start)
    if end:
        query = query.where(published <= end)
    return query


def _scope(s, scope, person):
    if person:
        notable.parse_key(person)
        return notable.people_ids(s, {person})
    return notable.scope_people(s, scope)


def trades(s, *, scope="watched", person=None, source=None, direction=None, ticker=None, issuer_id=None,
           published_from=None, published_to=None, limit=25, cursor=""):
    if published_from and published_to and published_from > published_to:
        raise UserError("notable.date_range_invalid")
    rows = _union(s, scope=scope, person=person, source=source, direction=direction, ticker=ticker,
                  issuer_id=issuer_id, published_from=published_from, published_to=published_to)
    if rows is None:
        return {"items": [], "total": 0, "next_cursor": None}
    ciks, rows = rows
    total = s.scalar(select(func.count()).select_from(rows))
    page = select(rows).order_by(rows.c.published.desc(), rows.c.rank.desc(), rows.c.tie.desc())
    if cursor:
        published, rank, tie = _decode(cursor)
        page = page.where(tuple_(rows.c.published, rows.c.rank, rows.c.tie)
                          < tuple_(literal(published), literal(rank), literal(tie)))
    found = s.execute(page.limit(limit + 1)).all()
    next_cursor = _encode(*found[limit - 1][1:4]) if len(found) > limit else None
    items = _hydrate(s, found[:limit], ciks, notable.keys(s))
    return {"items": items, "total": total, "next_cursor": next_cursor}


def _union(s, *, scope, person, source, direction, ticker, issuer_id, published_from, published_to, assets="all"):
    """The followed CIKs and the union of the matching rows' sort keys, or None when nothing matches."""
    ciks, ids = _scope(s, scope, person)
    key = ticker_tokens(ticker) if ticker else set()
    if ticker and len(key) != 1:
        raise UserError("notable.ticker_invalid")
    key = next(iter(key), None)
    queries = []
    codes, types = DIRECTIONS.get(direction, (("P", "S"), None)) if direction != "exchange" else ((), ("exchange",))
    if ciks and source in (None, "sec") and codes:
        queries.append(_sec_query(ciks, issuer_id=issuer_id, ticker=key, codes=codes,
                                  start=published_from, end=published_to))
    if ids and source in (None, "house", "oge"):
        tickers = {key} if key else None
        if issuer_id and not key:
            issuer = s.get(Issuer, issuer_id)
            tickers = ticker_tokens(issuer.ticker) if issuer else set()
        if tickers is None or tickers:
            queries.append(_political_query(ids, tickers=tickers, types=types, source=source,
                                            start=published_from, end=published_to, assets=assets))
    if not queries:
        return None
    return ciks, union_all(*queries).subquery()


def _hydrate(s, found, ciks, followed):
    sec_ids = [row.ref for row in found if row.source == "sec"]
    political_ids = [row.ref for row in found if row.source != "sec"]
    events = {}
    if sec_ids:
        for event, filing, issuer in s.execute(
                select(TransactionEvent, Filing, Issuer)
                .join(Filing, Filing.accession == TransactionEvent.accession)
                .join(Issuer, Issuer.id == TransactionEvent.issuer_id)
                .where(TransactionEvent.id.in_(sec_ids))):
            events[event.id] = (event, filing, issuer)
    owner_names = {}
    owner_ids = {owner for event, _, _ in events.values() for owner in event.owner_ids}
    if owner_ids:
        owner_names = dict(s.execute(select(Owner.id, Owner.name).where(Owner.id.in_(owner_ids))).all())
    political = {}
    if political_ids:
        for transaction, report, person in s.execute(
                select(PoliticalTransaction, PoliticalReport, PoliticalPerson)
                .join(PoliticalReport, PoliticalTransaction.report_id == PoliticalReport.id)
                .join(PoliticalPerson, PoliticalReport.person_id == PoliticalPerson.id)
                .where(PoliticalTransaction.id.in_(political_ids))):
            political[transaction.id] = (transaction, report, person)
    names = notable.default_map()
    items = []
    for row in found:
        if row.source == "sec":
            event, filing, issuer = events[row.ref]
            items.append(_sec_item(event, filing, issuer, ciks, followed, names, owner_names))
        else:
            items.append(_political_item(*political[row.ref], followed, names))
    # A government row's code opens the company page when an SEC issuer states it.
    tickers = {item["ticker"].upper().replace(".", "-") for item in items if item["source"] != "sec" and item["ticker"]}
    if tickers:
        issuers = {}
        for issuer, text in s.execute(select(Issuer.id, Issuer.ticker).where(states_any(Issuer.ticker, tickers))
                                      .order_by(Issuer.id)):
            for token in ticker_tokens(text) & tickers:
                issuers.setdefault(token, issuer)
        for item in items:
            if item["source"] != "sec" and item["ticker"]:
                item["issuer_id"] = issuers.get(item["ticker"].upper().replace(".", "-"))
    return items


def _person(key, name, followed, names, roles, title=None, district=None):
    known = names.get(key)
    return {"key": key, "name": (known or {}).get("name") or name, "watched": key in followed,
            "category": (known or {}).get("category"), "roles": roles, "title": title, "district": district}


def _sec_item(event, filing, issuer, ciks, followed, names, owner_names):
    data = event.data or {}
    owners = [owner for owner in event.owner_ids if owner in ciks] or list(event.owner_ids)
    relationships = data.get("owner_relationships") or {}
    listed = []
    for owner in owners:
        roles, title = sec_roles(relationships.get(owner))
        name = (data.get("owner_names") or {}).get(owner) or owner_names.get(owner) or owner
        listed.append(_person(f"sec:{owner}", name, followed, names, roles, title))
    published = filing.accepted_at.astimezone(ET).date()
    ticker = display_ticker(issuer.ticker, event.filing_ticker)
    raw_plan = str(data.get("raw_10b5_1_flag") or "").strip().lower()
    side = "buy" if event.transaction_code == "P" else "sell"
    return {
        "id": event.id, "source": "sec", "published": published, "published_at": filing.accepted_at,
        "person": listed[0], "others": listed[1:],
        "ticker": ticker, "asset_name": issuer.name, "issuer_id": issuer.id, "asset_kind": "stock",
        "side": side, "partial": False,
        "plan": True if event.is_plan else (False if raw_plan in ("0", "false") else None),
        "shares": shares_text(event.trade_shares), "price": price_text(event.reported_price),
        "amount": money_text(event.reported_amount), "amount_low": None, "amount_high": None,
        "currency": data.get("currency"),
        "transaction_date": event.transaction_date,
        "lag_days": (published - event.transaction_date).days if event.transaction_date else None,
        "url": filing.index_url or None, "accession": filing.accession, "report_id": None,
        "amendment": (filing.form or "").upper().endswith("/A"),
        "owner": None, "ocr": False, "review": [],
        **_price_code(ticker, "stock"), "published_basis": published_basis(filing.accepted_at),
    }


def _political_item(transaction, report, person, followed, names):
    published = report.report_date or report.filed_date
    key = notable.person_key(person.source, name=person.name, district=person.district, position=person.position)
    roles = ["house"] if person.source == "house" else [oge_role(person.position)]
    warnings = set(transaction.warnings or [])
    return {
        "id": transaction.id, "source": report.source, "published": published, "published_at": None,
        "person": _person(key, person.name, followed, names, roles,
                          person.position if person.source == "oge" else None, person.district),
        "others": [],
        "ticker": transaction.ticker, "asset_name": transaction.asset_name, "issuer_id": None,
        "asset_kind": asset_kind(transaction.asset_type),
        "side": {"purchase": "buy", "sale": "sell", "partial_sale": "sell", "exchange": "exchange"}.get(
            transaction.transaction_type, "unknown"),
        "partial": transaction.transaction_type == "partial_sale",
        "plan": None, "shares": None, "price": None, "amount": None,
        "amount_low": money_text(transaction.amount_low), "amount_high": money_text(transaction.amount_high),
        "currency": None,
        "transaction_date": transaction.transaction_date,
        "lag_days": (published - transaction.transaction_date).days
        if published and transaction.transaction_date else None,
        "url": report.url or None, "accession": None, "report_id": report.id,
        "amendment": report.amendment_status == "linked",
        "owner": transaction.owner if transaction.owner != "unknown" else None,
        "ocr": bool(warnings & {"ocr", "text_layer_unusable"}),
        "review": review_reasons(transaction, published),
        **_price_code(transaction.ticker, asset_kind(transaction.asset_type)),
        "published_basis": published_basis(published=published),
    }


def person_view(s, key):
    """One person: who they are, how their local records match, and their original filings."""
    from iirp.models import NotablePerson

    parts = notable.parse_key(key)
    row = s.get(NotablePerson, key)
    political = notable.political_people(s, {key}).get(key, []) if parts["source"] != "sec" else []
    owner = s.get(Owner, parts["cik"]) if parts["source"] == "sec" else None
    if not row and not political and not owner and key not in notable.default_map():
        raise NotFoundError("notable.person_not_found")
    view = notable.entry(key, name=row.name if row else (owner.name if owner else None),
                         added_at=row.added_at if row else None, local=owner is not None, political=political)
    reports, filings = [], []
    if political:
        for report in s.scalars(select(PoliticalReport).where(
                PoliticalReport.person_id.in_([person.id for person in political]))
                .order_by(func.coalesce(PoliticalReport.report_date, PoliticalReport.filed_date).desc().nulls_last(),
                          PoliticalReport.id)):
            reports.append({
                "id": report.id, "report_date": report.report_date or report.filed_date, "url": report.url,
                "file_url": f"/api/v1/political/reports/{report.id}/file" if report.source_hash else None,
                "amendment": report.amendment_status in ("linked", "unresolved"),
                "superseded": report.status == "superseded", "status": report.status,
                "ocr_pages": report.ocr_pages, "page_count": report.page_count,
            })
        view["position"] = political[0].position if parts["source"] == "oge" else None
        view["roles"] = ["house"] if parts["source"] == "house" else [oge_role(political[0].position)]
    elif parts["source"] == "oge":
        view["roles"] = [oge_role(parts["position"])]
    elif parts["source"] == "house":
        view["roles"] = ["house"]
    else:
        latest = s.execute(
            select(TransactionEvent.data["owner_relationships"][parts["cik"]], Filing.accepted_at)
            .join(Filing, Filing.accession == TransactionEvent.accession)
            .where(TransactionEvent.owner_ids.has_any(cast([parts["cik"]], ARRAY(Text))))
            .order_by(Filing.accepted_at.desc().nulls_last()).limit(1)).first()
        roles, title = sec_roles(latest[0] if latest else None)
        view["roles"], view["title"] = roles, title
        count, last = s.execute(
            select(func.count(func.distinct(TransactionEvent.accession)), func.max(Filing.accepted_at))
            .join(Filing, Filing.accession == TransactionEvent.accession)
            .where(TransactionEvent.owner_ids.has_any(cast([parts["cik"]], ARRAY(Text))))).one()
        filings = {"count": count, "latest_at": last,
                   "edgar_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={parts['cik']}"
                                "&type=4&dateb=&owner=include&count=40"}
    view.setdefault("title", None)
    return {**view, "reports": reports, "filings": filings or None}


class _Ref:
    def __init__(self, source, ref):
        self.source, self.ref = source, ref


def political_trade(s, transaction_id):
    """One government row with its report, as filed: the original text, page and row, and
    the option terms, for the trade detail page."""
    found = s.execute(select(PoliticalTransaction, PoliticalReport)
                      .join(PoliticalReport, PoliticalTransaction.report_id == PoliticalReport.id)
                      .where(PoliticalTransaction.id == transaction_id)).first()
    if found is None:
        raise NotFoundError("notable.trade_not_found")
    transaction, report = found
    item = _hydrate(s, [_Ref(report.source, transaction.id)], set(), notable.keys(s))[0]
    return {
        "trade": item,
        "raw_row": transaction.raw_row, "page": transaction.page, "row_number": transaction.row_number,
        "asset_type": transaction.asset_type, "ticker_origin": transaction.ticker_origin,
        "option_type": transaction.option_type, "strike_price": price_text(transaction.strike_price),
        "expiration_date": transaction.expiration_date, "notification_date": transaction.notification_date,
        "report": {
            "id": report.id, "report_date": report.report_date, "filed_date": report.filed_date,
            "url": report.url,
            "file_url": f"/api/v1/political/reports/{report.id}/file" if report.source_hash else None,
            "superseded": report.status == "superseded",
            "amendment": report.amendment_status in ("linked", "unresolved"),
            "ocr_pages": report.ocr_pages, "page_count": report.page_count,
        },
    }


# Below this many values a summary lists them instead of a median.
SMALL = 5
# The newest rows a summary reads.
SUMMARY_ROWS = 5000


def _describe(values, pending, missing):
    ordered = sorted(values)
    small = len(ordered) < SMALL
    return {"count": len(ordered), "up": sum(value > 0 for value in ordered),
            "median": None if small or not ordered else str(median(ordered)),
            "values": [str(value) for value in ordered] if small else [],
            "pending": pending, "missing": missing}


def person_summary(s, key, n):
    """Descriptive statistics of one person's coded stock trades: for buys and for sales,
    the change n sessions after the trade date and after the publication date, with
    N, how many rose, and the median (N below five lists the values instead). Windows
    are the Insider windows from prices already stored; trades still without prices
    are counted, not fetched."""
    notable.parse_key(key)
    union = _union(s, scope="watched", person=key, source=None, direction=None, ticker=None, issuer_id=None,
                   published_from=None, published_to=None)
    sides = {side: {"trades": 0, "after": None, "published": None} for side in ("buy", "sell")}
    result = {"n": n, "small": SMALL, "sides": sides, "no_code": 0, "rows": 0}
    if union is None:
        for side in sides.values():
            side["after"] = side["published"] = _describe([], 0, 0)
        return result
    ciks, rows = union
    found = s.execute(select(rows).order_by(rows.c.published.desc(), rows.c.rank.desc(), rows.c.tie.desc())
                      .limit(SUMMARY_ROWS)).all()
    items = [item for item in _hydrate(s, found, ciks, set()) if item["side"] in sides]
    result["rows"] = len(items)
    coded = [item for item in items if item["price_ticker"] and item["transaction_date"]]
    result["no_code"] = len(items) - len(coded)
    keys = []
    for item in coded:
        keys.append({"ticker": item["price_ticker"], "date": item["transaction_date"].isoformat(),
                     "issuer_id": item["issuer_id"]})
        keys.append({"ticker": item["price_ticker"],
                     "date": (item["published_basis"] or item["transaction_date"]).isoformat(),
                     "issuer_id": item["issuer_id"]})
    windows = trade_windows(s, keys, n)["items"] if keys else []
    collected = {side: {anchor: ([], [0], [0]) for anchor in ("after", "published")} for side in sides}
    for position, item in enumerate(coded):
        sides[item["side"]]["trades"] += 1
        for anchor, window in (("after", windows[2 * position]), ("published", windows[2 * position + 1])):
            values, pending, missing = collected[item["side"]][anchor]
            change = window.get("after") or {}
            if change.get("status") == "available":
                values.append(Decimal(change["value"]))
            elif change.get("status") in ("pending", "intraday"):
                pending[0] += 1
            else:
                missing[0] += 1
    for side, anchors in collected.items():
        for anchor, (values, pending, missing) in anchors.items():
            sides[side][anchor] = _describe(values, pending[0], missing[0])
    return result
