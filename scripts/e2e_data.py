"""Small, synthetic browser fixtures; never reads or writes an installation database."""

import hashlib
import json
import runpy
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from iirp.analysis.calendar import last_completed_session, session_bounds, sessions
from iirp.analysis.event_windows import analyze_events
from iirp.analysis.research import compute_research
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.insider.facts import persist_document
from iirp.insider.records import resolve_amendment
from iirp.jobs.batches import defaults
from iirp.jobs.queue import ensure_defaults
from iirp.market.instruments import INSTRUMENTS
from iirp.market.stock_quotes import stock_quote, stock_session
from iirp.models import (
    AmendmentRelation,
    AnalysisRequest,
    AnalysisResult,
    Base,
    Batch,
    CollectionStrategy,
    EventSet,
    IndexConstituent,
    LongPriceBar,
    LongPriceSeries,
    MarketQuote,
    RequestScope,
    Security,
    SourceObject,
    TransactionEvent,
    WatchlistItem,
    now,
)
from sqlalchemy import select, text
from sqlalchemy.engine import make_url


def require_test_database():
    import re

    url = make_url(settings().database_url)
    if (not re.fullmatch(r"iirp_v1_test_[a-z0-9_]{1,40}", url.database or "")
            or not re.fullmatch(r"(?:127\.0\.0\.1|localhost|iirp-e2e-[a-f0-9]{12}-pg)", url.host or "")):
        raise SystemExit("Browser fixtures require an iirp_v1_test_* database")


def seed_notable(s, stamp, cutoff):
    """The default followed list, the synthetic SEC filer and one synthetic House member
    whose report has an electronic row, a scanned row and a row with a stated doubt."""
    from decimal import Decimal

    from iirp.models import NotablePerson
    from iirp.models.political import PoliticalPerson, PoliticalReport, PoliticalTransaction
    from iirp.notable.people import seed as seed_people

    seed_people(s)
    s.add(NotablePerson(key="sec:0000000456", source="sec", name="SYNTHETIC Owner A", added_at=stamp - timedelta(days=30)))
    s.add(NotablePerson(key="house:SYNTHETIC Member|ZZ01", source="house", name="SYNTHETIC Member",
                        added_at=stamp - timedelta(days=30)))
    member = PoliticalPerson(source="house", identity="SYNTHETIC Member|ZZ01|representative", name="SYNTHETIC Member",
                             district="ZZ01", position="representative")
    other = PoliticalPerson(source="house", identity="SYNTHETIC Other|ZZ02|representative", name="SYNTHETIC Other",
                            district="ZZ02", position="representative")
    s.add_all([member, other])
    s.flush()
    for person, external in ((member, "synthetic-1"), (other, "synthetic-2")):
        report = PoliticalReport(source="house", external_id=external, person_id=person.id, filed_date=cutoff,
                                 report_date=cutoff, report_date_source="received_date",
                                 url=f"https://disclosures-clerk.house.gov/{external}.pdf", status="review",
                                 page_count=1, pages_done=1, ocr_pages=1)
        s.add(report)
        s.flush()
        rows = [("DEMO", "SYNTHETIC DEMO - Common Stock", "purchase", [], "reported"),
                ("SAMP", "SYNTHETIC SAMP - Common Stock", "sale", ["ocr"], "reported"),
                ("TEST", "SYNTHETIC TEST Inc", "unknown", ["ocr", "unknown_transaction_type"], "inferred"),
                ("DEMO", "SYNTHETIC small stock", "purchase", [], "reported"),
                (None, "SYNTHETIC municipal bond", "purchase", [], None)]
        for number, (ticker, asset, kind, warnings, origin) in enumerate(rows, 1):
            s.add(PoliticalTransaction(report_id=report.id, page=1, row_number=number, owner="self", asset_name=asset,
                                       ticker=ticker, ticker_origin=origin, asset_type="bond" if number == 5 else "stock", transaction_type=kind,
                                       transaction_date=cutoff - timedelta(days=12), amount_low=Decimal("1001" if number == 4 else "15001"),
                                       amount_high=Decimal("15000" if number == 4 else "50000"), raw_row=asset,
                                       confidence="review" if warnings else "parsed", warnings=warnings))


def seed_notifications(s, stamp, cutoff):
    """Notifications turned on a day ago, phone pushes off, and three notifications: the
    synthetic filer's purchase, the synthetic member's report and a cluster of buyers;
    the oldest is read."""
    from iirp.models import TransactionEvent
    from iirp.models.notifications import Notification
    from iirp.notifications import settings as notification_settings

    s.flush()
    notification_settings.update(s, {"enabled": True}, at=stamp - timedelta(days=1))
    event = s.scalar(select(TransactionEvent.id).where(TransactionEvent.issuer_id == "0000000123").limit(1))
    published, traded = str(cutoff), str(cutoff - timedelta(days=7))
    s.add_all([
        Notification(dedupe_key="rule_cluster:0000000123:synthetic", kind="rule_cluster", issuer_id="0000000123",
                     created_at=stamp - timedelta(hours=3), read_at=stamp - timedelta(hours=2), data={
                         "source": "sec", "ticker": "DEMO", "asset_name": "SYNTHETIC DEMO", "issuer_id": "0000000123",
                         "people": 3, "names": ["SYNTHETIC Owner A", "SYNTHETIC Owner B", "SYNTHETIC Owner C"],
                         "rows": 3, "amount": "75000.00", "days": 7, "traded_from": traded, "traded_to": traded,
                         "published": published}),
        Notification(dedupe_key="trade:political:synthetic-1", kind="trade", person_key="house:SYNTHETIC Member|ZZ01",
                     created_at=stamp - timedelta(hours=2), data={
                         "source": "house", "person": {"key": "house:SYNTHETIC Member|ZZ01", "name": "SYNTHETIC Member",
                                                       "roles": ["house"], "district": "ZZ01"},
                         "buys": 1, "sells": 1, "rows": 3, "items": [], "traded_from": traded, "traded_to": traded,
                         "published": published, "url": "https://disclosures-clerk.house.gov/synthetic-1.pdf",
                         "amendment": False, "trade_id": None}),
        Notification(dedupe_key="trade:sec:synthetic", kind="trade", person_key="sec:0000000456", issuer_id="0000000123",
                     created_at=stamp - timedelta(hours=1), data={
                         "source": "sec", "person": {"key": "sec:0000000456", "name": "SYNTHETIC Owner A", "roles": ["ceo"]},
                         "ticker": "DEMO", "asset_name": "SYNTHETIC DEMO", "issuer_id": "0000000123",
                         "buy": {"rows": 1, "amount": "25250.00"}, "sell": None, "traded_from": traded,
                         "traded_to": traded, "published": published, "amendment": False, "trade_id": event, "rows": 1}),
    ])


def seed(output: Path):
    require_test_database()
    # Preserve synthetic daily history between clock scenarios. Only a newly
    # closed session needs appending; requests, results, filings and preferences
    # start fresh, without inserting the same history three times.
    retained = {"issuer", "security", "security_identifier", "long_price_series", "long_price_bar"}
    # Also includes installed extensions' models, loaded by the migration entry point.
    with engine().begin() as connection:
        connection.execute(text("TRUNCATE " + ",".join(
            '"' + table.name + '"' for table in Base.metadata.sorted_tables if table.name not in retained) + " CASCADE"))
    ensure_defaults()
    source_dir = settings().runtime_dir / "synthetic"
    source_dir.mkdir(parents=True, exist_ok=True)
    stamp = now()
    cutoff = last_completed_session(as_of=stamp)
    first = date(stamp.year - 10, 1, 1)
    days = sessions(first, cutoff)
    bars = [{"date": day.isoformat(), "open": str(25 + i % 30 / 100),
             "high": "26", "low": "24", "close": str(25.1 + i % 30 / 100),
             "volume": "100000", "status": "VALID"} for i, day in enumerate(days)]
    with session() as s, s.begin():
        defaults(s)
        for strategy in s.scalars(select(CollectionStrategy)):
            strategy.enabled = False
        xml_template = (ROOT / "scripts/e2e/ownership.xml").read_text()
        # Use the real parser and fact publication, including a changed-source revision.
        for index, ticker in enumerate(["DEMO", "SAMP", "TEST"]):
            xml = (xml_template.replace("2026-08-31", str(cutoff - timedelta(days=7)))
                   .replace("DEMO", ticker).replace("<issuerCik>123", f"<issuerCik>{123 + index}")
                   .replace("<transactionCode>P", "<transactionCode>S" if index == 1 else "<transactionCode>P"))
            if index == 1:
                xml = xml.replace("<transactionAcquiredDisposedCode><value>A",
                                  "<transactionAcquiredDisposedCode><value>D")
            accession = f"0000000123-26-{index + 1:06d}"
            for revision in range(2 if index == 0 else 1):
                content = xml.replace("25.25", "25.30") if revision else xml
                digest = hashlib.sha256(content.encode()).hexdigest()
                (source_dir / digest).write_text(content)
                s.add(SourceObject(sha256=digest, relative_path="synthetic/" + digest,
                                   byte_size=len(content), media_type="application/xml"))
                s.flush()
                url = f"https://www.sec.gov/Archives/edgar/data/123/{accession.replace('-', '')}/ownership.xml"
                persist_document(s, SimpleNamespace(id=None, target={}), {"filing": {
                    "accession": accession, "form": "4", "accepted_at": (stamp - timedelta(days=2)).isoformat(),
                    "filing_date": str(cutoff - timedelta(days=1)), "document_url": url,
                    "xml_payload": content, "xml_encoding": "utf8", "xml_sha256": digest,
                }}, {url: digest})
            if index == 0:
                original = s.scalar(select(TransactionEvent).where(
                    TransactionEvent.issuer_id == "0000000123", TransactionEvent.status == "CURRENT",
                    TransactionEvent.transaction_code == "P"))
                content = xml.replace("<documentType>4</documentType>", "<documentType>4/A</documentType>").replace("25.25", "25.35")
                digest = hashlib.sha256(content.encode()).hexdigest()
                (source_dir / digest).write_text(content)
                s.add(SourceObject(sha256=digest, relative_path="synthetic/" + digest,
                                   byte_size=len(content), media_type="application/xml"))
                s.flush()
                url = "https://www.sec.gov/Archives/edgar/data/123/000000012326000010/ownership.xml"
                persist_document(s, SimpleNamespace(id=None, target={}), {"filing": {
                    "accession": "0000000123-26-000010", "form": "4/A",
                    "accepted_at": (stamp - timedelta(days=1)).isoformat(),
                    "filing_date": str(cutoff), "document_url": url, "xml_payload": content,
                    "xml_encoding": "utf8", "xml_sha256": digest,
                }}, {url: digest})
                relation = next(item for item in s.scalars(select(AmendmentRelation).where(AmendmentRelation.action == "UNCONFIRMED", AmendmentRelation.accession == "0000000123-26-000010"))
                                if s.get(TransactionEvent, item.amended_event_id).transaction_code == "P")
                resolve_amendment(s, relation.id, "replace", original.id, {"note": "SYNTHETIC correction"})
            s.add(IndexConstituent(index_name="sp500", ticker=ticker, cik=f"{123 + index:010d}",
                                   name=f"SYNTHETIC {ticker}", industry="Information Technology",
                                   sub_industry="Application Software", classification="GICS", updated_at=stamp))
        from iirp.analysis.sectors import group_etfs, sector_etfs

        symbols = list(dict.fromkeys(["DEMO", "SAMP", "TEST", "^GSPC", "^NDX", "SPY", "QQQ",
                                      *sector_etfs(), *group_etfs()]))
        securities = {}
        for ticker in symbols:
            security = s.scalar(select(Security).where(Security.symbol == ticker))
            if security is None:
                security = Security(symbol=ticker, name=f"SYNTHETIC {ticker}", instrument="EQUITY",
                                issuer_id=f"{123 + ['DEMO', 'SAMP', 'TEST'].index(ticker):010d}" if ticker in ["DEMO", "SAMP", "TEST"] else None,
                                currency="USD", exchange="NYSE", calendar="XNYS", status="VERIFIED")
            s.add(security)
            s.flush()
            securities[ticker] = security
            series = s.scalar(select(LongPriceSeries).where(LongPriceSeries.security_id == security.id))
            previous = series.complete_through if series else None
            if series is None:
                series = LongPriceSeries(security_id=security.id, symbol=ticker, retain_from=first,
                                     start_date=first, end_date=cutoff, complete_through=cutoff,
                                     needs_full=False, fetched_at=stamp, attempted_at=stamp)
            series.end_date = series.complete_through = cutoff
            series.fetched_at = series.attempted_at = stamp
            s.add(series)
            s.flush()
            new_bars = [{"cache_id": series.id, "session_date": day,
                "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
                "volume": 100000, "status": "VALID"} for day, bar in zip(days, bars, strict=True)
                if previous is None or day > previous]
            if new_bars:
                s.execute(LongPriceBar.__table__.insert(), new_bars)
        for instrument in INSTRUMENTS:
            symbol = instrument.symbol
            s.add(MarketQuote(symbol=symbol, fetched_at=stamp, data={"symbol": symbol,
                "value": 25.5, "previous_close": "25", "change": 0.5, "change_percent": 2.0, "status": "LIVE",
                "quote_time": stamp.isoformat(), "market_state": "REGULAR", "currency": "USD"}))
        # Stock quotes for the feed's companies, so trade rows show a current price and
        # the home quote line its states: DEMO is live in the session and closed at the
        # weekend, SAMP lags hours behind and is due again (delayed), TEST has none.
        period, _ = stock_session(stamp)
        closing = session_bounds(last_completed_session(stamp))[1]
        regular = stamp - timedelta(minutes=1) if period == "regular" else min(closing - timedelta(seconds=2), stamp)
        for symbol, value, quoted in [("DEMO", 25.4, regular), ("SAMP", 24.9, regular - timedelta(hours=3))]:
            data = stock_quote(symbol, {"regularMarketPrice": value, "regularMarketTime": quoted.timestamp(),
                                        "regularMarketPreviousClose": 25, "currency": "USD"}, stamp)
            if symbol == "SAMP":
                data["next_refresh_at"] = (stamp - timedelta(minutes=1)).isoformat()
            s.add(MarketQuote(symbol=symbol, fetched_at=stamp, data=data))
        s.add_all(WatchlistItem(ticker=ticker, added_at=stamp - timedelta(days=10)) for ticker in ["DEMO", "SAMP"])
        seed_notable(s, stamp, cutoff)
        seed_notifications(s, stamp, cutoff)
        ids = {}
        for kind in ["monthly", "event_dates"]:
            params = {"kind": kind, "tickers": ["DEMO"], "historical_years": 2,
                      "current_year": stamp.year, "cutoff_date": str(cutoff), "benchmark": None}
            if kind == "event_dates":
                events = [{"ticker": "DEMO", "date": str(cutoff - timedelta(days=30)),
                           "session": "during", "name": "SYNTHETIC event", "context": []}]
                event_set = EventSet(kind="custom", title="SYNTHETIC events", events=events)
                s.add(event_set)
                s.flush()
                params.update(event_kind="custom", events=events, n=5, title=event_set.title,
                              event_set_id=event_set.id)
            batch = Batch(request_id="synthetic-" + kind, scope_key=hashlib.sha256(kind.encode()).hexdigest(),
                          kind="market_history", title="SYNTHETIC analysis", params=params,
                          trigger="manual", status="SUCCEEDED")
            s.add(batch)
            s.flush()
            request = AnalysisRequest(batch_id=batch.id, params=params)
            s.add(request)
            s.flush()
            security = securities["DEMO"]
            s.add(RequestScope(batch_id=batch.id, symbol="DEMO", security_id=security.id,
                               start_date=first, end_date=cutoff, status="READY"))
            result = (compute_research(params, bars, today=cutoff) if kind == "monthly" else
                      analyze_events(params["events"], bars, n=5, cutoff=cutoff, kind="custom"))
            if kind == "event_dates":
                result.update(symbol="DEMO", price_fetched_at=stamp.isoformat())
            s.add(AnalysisResult(analysis_id=request.id, security_id=security.id, input_key=kind,
                                inputs={"price_fetched_at": stamp.isoformat()}, data=result,
                                expires_at=stamp + timedelta(hours=24)))
            ids[kind] = request.id
        s.flush()
        ids["transaction"] = s.scalar(select(TransactionEvent.id).where(
            TransactionEvent.status == "CURRENT", TransactionEvent.transaction_code == "P").limit(1))
        for path in sorted((ROOT / "frontend/src/features").glob("*/e2e/seed.py")):
            ids.update(runpy.run_path(str(path))["seed"](s, stamp, cutoff, securities))
    output.write_text(json.dumps({**ids, "clock": stamp.isoformat()}))
