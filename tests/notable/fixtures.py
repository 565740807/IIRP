"""Synthetic SEC filings and government reports for the followed-people tests."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from iirp.insider.typed import typed_fields
from iirp.models import (
    Filing,
    FilingOwner,
    FilingVersion,
    Issuer,
    Owner,
    SourceObject,
    TransactionEvent,
)
from iirp.models.political import PoliticalPerson, PoliticalReport, PoliticalTransaction

AT = datetime(2026, 10, 8, 18, tzinfo=timezone.utc)


def sec_trade(s, *, owner="0001767470", owner_name="Cohen Ryan", issuer="0001326380", ticker="GME",
              code="P", day=date(2026, 10, 6), accepted=AT, shares="1000", price="25", plan=None,
              roles=None, form="4", status="CURRENT", accession=None, extra_owners=()):
    """One Form 4 row; the relationship flags are the filed ones (officer title, director, 10%)."""
    if s.get(Issuer, issuer) is None:
        s.add(Issuer(id=issuer, name=f"Issuer {ticker}", ticker=ticker))
    owners = [owner, *extra_owners]
    for identifier in owners:
        if s.get(Owner, identifier) is None:
            s.add(Owner(id=identifier, name=owner_name if identifier == owner else f"Owner {identifier}"))
    s.flush()
    accession = accession or uuid.uuid4().hex[:20]
    if s.get(Filing, accession) is None:
        digest = uuid.uuid4().hex * 2
        s.add(SourceObject(sha256=digest, relative_path=f"synthetic/{digest}", byte_size=0,
                           media_type="application/xml"))
        s.add(Filing(accession=accession, form=form, issuer_id=issuer, accepted_at=accepted, visible=True,
                     index_url=f"https://www.sec.gov/Archives/edgar/data/{int(issuer)}/{accession}-index.htm"))
        s.flush()
        version = FilingVersion(accession=accession, source_hash=digest, parser_version="synthetic", data={})
        s.add(version)
        s.flush()
    else:
        version = s.query(FilingVersion).filter_by(accession=accession).one()
    relationship = {"is_officer": "1", "officer_title": "Chief Executive Officer", "is_director": "1",
                    **(roles or {})}
    for identifier in owners:
        if s.get(FilingOwner, (version.id, identifier)) is None:
            s.add(FilingOwner(version_id=version.id, owner_id=identifier, relationship=relationship))
    data = {"table": "I", "code": code, "direction": "A" if code == "P" else "D", "shares": shares,
            "price_per_share": price, "direct_or_indirect": "D", "ticker": ticker,
            "raw_10b5_1_flag": {True: "1", False: "0", None: None}[plan],
            "owner_names": {owner: owner_name}, "owner_relationships": {o: relationship for o in owners}}
    rows = s.query(TransactionEvent).filter_by(accession=accession).count()
    event = TransactionEvent(issuer_id=issuer, accession=accession, version_id=version.id,
                             row_key=f"I:{rows}", transaction_date=day, accepted_at=accepted,
                             owner_ids=owners, data=data, status=status, **typed_fields(data, [relationship]))
    s.add(event)
    s.flush()
    return event


def politician(s, *, source="house", name="Nancy Pelosi", district="CA11", position=None):
    position = position or ("representative" if source == "house" else None)
    identity = "|".join([name, district or "", position or ""])
    person = s.query(PoliticalPerson).filter_by(source=source, identity=identity).one_or_none()
    if person is None:
        person = PoliticalPerson(source=source, identity=identity, name=name,
                                 district=district if source == "house" else None, position=position)
        s.add(person)
        s.flush()
    return person


def report(s, person, *, report_date=date(2026, 10, 7), status="parsed", amendment_status="original",
           amendment_of=None, rows=(), discovered_at=AT):
    """A parsed report with its rows: (ticker, asset, type, date, low, high, warnings, origin)."""
    item = PoliticalReport(source=person.source, external_id=uuid.uuid4().hex, person_id=person.id,
                           filed_date=report_date, report_date=report_date, url=f"https://example.gov/{uuid.uuid4().hex}.pdf",
                           status=status, amendment_status=amendment_status, amendment_of_id=amendment_of,
                           page_count=1, pages_done=1, discovered_at=discovered_at)
    s.add(item)
    s.flush()
    for number, row in enumerate(rows, 1):
        ticker, asset, kind, day, low, high, *rest = row
        warnings = rest[0] if rest else []
        origin = rest[1] if len(rest) > 1 else ("reported" if ticker else None)
        s.add(PoliticalTransaction(
            report_id=item.id, page=1, row_number=number, owner="self", asset_name=asset, ticker=ticker,
            ticker_origin=origin, asset_type="bond" if "bond" in asset.lower() else "stock",
            transaction_type=kind, transaction_date=day,
            amount_low=Decimal(low) if low else None, amount_high=Decimal(high) if high else None,
            raw_row=asset, confidence="review" if warnings else "parsed", warnings=list(warnings)))
    s.flush()
    return item
