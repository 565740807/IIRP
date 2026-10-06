"""Provider identity checks before a response becomes the price cache (real PostgreSQL)."""

import pytest
from iirp.db import session
from iirp.models import SourceObject
from test_market_lifecycle import (  # noqa: F401
    bar,
    cached,
    clean_market,
    commit_prices,
    market_database,
    response,
)
from test_market_lifecycle import (
    security_id as security_fixture,
)

security_id = security_fixture


@pytest.mark.parametrize("metadata", [
    {"symbol": "OTHER"}, {"currency": "EUR"}, {"instrumentType": "ETF"},
])
def test_explicit_identity_conflict_keeps_the_previous_cache(security_id, metadata):
    commit_prices(security_id, response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")]))
    old = cached(security_id)
    payload = response([bar("2023-01-03", "99"), bar("2023-01-04", "99"), bar("2023-01-05", "99")])
    payload["metadata"] = metadata
    result, source = commit_prices(security_id, payload)
    assert not result["cached"] and "冲突" in result["reason"]
    assert cached(security_id).id == old.id
    with session() as s:
        assert s.get(SourceObject, source["sha256"])


@pytest.mark.parametrize("metadata", [{}, {"symbol": None, "currency": None},
                                      {"symbol": " synth ", "currency": "USD", "instrumentType": "equity"}])
def test_missing_identity_and_canonical_case_are_compatible(security_id, metadata):
    payload = response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")])
    payload["metadata"] = metadata
    result, _ = commit_prices(security_id, payload)
    assert result["cached"]


def test_provider_alias_requires_same_security_mapping(security_id):
    from datetime import date

    from iirp.business_models import SecurityIdentifier

    with session() as s, s.begin():
        s.add(SecurityIdentifier(security_id=security_id, provider="synthetic", symbol="SYNTH-A", valid_from=date(2020, 1, 1)))
    payload = response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")])
    payload["metadata"] = {"symbol": "SYNTH-A", "currency": "USD", "instrumentType": "EQUITY"}
    payload["symbol"] = "SYNTH"
    result, _ = commit_prices(security_id, payload)
    assert result["cached"]
    payload["symbol"] = "WRONG"
    result, _ = commit_prices(security_id, payload)
    assert not result["cached"]


def test_currency_unit_case_is_not_a_legal_alias(security_id):
    from iirp.business_models import Security

    with session() as s, s.begin():
        s.get(Security, security_id).currency = "GBP"
    payload = response([bar("2023-01-03"), bar("2023-01-04"), bar("2023-01-05")])
    payload["metadata"] = {"currency": "GBp"}
    result, _ = commit_prices(security_id, payload)
    assert not result["cached"]
    assert "冲突" in result["reason"]
