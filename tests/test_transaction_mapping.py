"""Verified transaction mappings keep issuer, class, validity, and revision boundaries."""

from datetime import date
from uuid import uuid4

import pytest
from iirp.business_models import Security, SecurityIdentifier, TransactionEvent
from iirp.db import session
from iirp.lifecycle import map_transaction_security, transaction_detail
from sqlalchemy import select
from test_sec_facts import isolated_database, save  # noqa: F401


def _seed():
    with session() as s, s.begin():
        accession = f"0000000123-26-{uuid4().int % 1000000:06d}"
        save(s, accession=accession)
        event = s.scalar(select(TransactionEvent).where(TransactionEvent.accession == accession, TransactionEvent.data["code"].astext == "P"))
        assert event and event.data["security_title"] == "Common Stock"
        security = Security(symbol="DEMO", name="SYNTHETIC Example Issuer", issuer_id=event.issuer_id,
                            instrument="EQUITY", status="VERIFIED", calendar="XNYS")
        s.add(security)
        s.flush()
        s.add(SecurityIdentifier(security_id=security.id, provider="synthetic-test-" + accession[-6:], symbol="DEMO",
                                 valid_from=date(2026, 1, 1)))
        return event.id, security.id, dict(event.data)


def test_mapping_revision_is_a_candidate_review_with_prior_version_available():
    event_id, security_id, original = _seed()
    first = map_transaction_security(event_id, {"security_id": security_id, "evidence": "synthetic SEC source and issuer class"})["data"]["transaction"]
    assert first["mapping_version"] == 1 and first["security_id"] == security_id
    assert first["security_mapping"]["identifier_valid_from"] == "2026-01-01"
    same = map_transaction_security(event_id, {"security_id": security_id, "evidence": "synthetic SEC source and issuer class"})["data"]["transaction"]
    assert same["mapping_version"] == 1
    second = map_transaction_security(event_id, {"security_id": security_id, "evidence": "synthetic corrected source detail"})["data"]["transaction"]
    assert second["mapping_version"] == 2 and len(second["security_mapping_history"]) == 2
    old = transaction_detail(event_id, mapping_version=1)["data"]["transaction"]
    assert old["mapping_version"] == 1 and old["security_mapping"]["evidence"] == "synthetic SEC source and issuer class"
    with session() as s:
        persisted = s.get(TransactionEvent, event_id)
        assert {k:v for k,v in persisted.data.items() if k != "security_mapping"} == original
    with pytest.raises(LookupError, match="版本不存在"):
        transaction_detail(event_id, mapping_version=3)


def test_mapping_rejects_unbound_issuer_even_with_matching_ticker():
    event_id, security_id, _ = _seed()
    with session() as s, s.begin():
        s.get(Security, security_id).issuer_id = None
    with pytest.raises(ValueError, match="同一发行人"):
        map_transaction_security(event_id, {"security_id": security_id, "evidence": "synthetic ticker match only"})


def test_mapping_requires_a_valid_identifier_and_exact_common_stock():
    event_id, security_id, _ = _seed()
    with session() as s, s.begin():
        ident = s.scalar(select(SecurityIdentifier).where(SecurityIdentifier.security_id == security_id))
        ident.valid_to = date(2026, 1, 31)
    with pytest.raises(ValueError, match="有效的证券标识符"):
        map_transaction_security(event_id, {"security_id": security_id, "evidence": "synthetic evidence"})


def test_export_freezes_mapping_version():
    import json

    from iirp.research_api import transaction_export
    event_id, security_id, _ = _seed()
    with pytest.raises(Exception, match="尚未核对"):
        transaction_export(event_id)
    map_transaction_security(event_id, {"security_id": security_id, "evidence": "synthetic SEC source and issuer class"})
    result = transaction_export(event_id, format="json")
    payload = json.loads(result.body)
    assert payload["transaction"]["mapping_version"] == 1
    assert payload["price_context"]["transaction"]["original_date"] == "2026-08-31"
    assert payload["price_context"]["disclosure"] is not None
    csv = transaction_export(event_id, format="csv").body.decode()
    assert "time_basis" in csv and "mapping_version" in csv
    pinned = json.loads(transaction_export(event_id, format="json", cutoff_date=date(2026, 9, 2)).body)
    assert pinned["price_context"]["cutoff_date"] == "2026-09-02"
    assert pinned["price_context"]["transaction"]["day_5_status"] == "not_yet_formed"
