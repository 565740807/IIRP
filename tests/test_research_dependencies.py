"""Immutable research dependencies: real DB, exclusively synthetic isolated data."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from iirp import lifecycle
from iirp.business_models import AnalysisRequest, DatasetBar, MarketBar, PriceDataset, Security
from iirp.db import session
from iirp.models import now
from sqlalchemy import select
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)
from test_performance_pipeline import extend_prices, params


def _key(request_id, dataset_id, security_id):
    with session() as s:
        return lifecycle.research_input_key(s.get(AnalysisRequest, request_id), s.get(PriceDataset, dataset_id), [], s.get(Security, security_id))


def test_outside_interval_update_does_not_invalidate_research():
    security = seed_security()
    old = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5))
    research = lifecycle.create_analysis(params(kind='interval', start_mmdd='01-03', end_mmdd='01-05'))
    before = _key(research['id'], old, security)
    latest = extend_prices(security, old, date(2023, 6, 1), date(2023, 6, 2))
    assert _key(research['id'], latest, security) == before


def test_missing_in_range_session_changes_dependency():
    security = seed_security()
    old = seed_prices(security, date(2023, 1, 3), date(2023, 1, 4))
    research = lifecycle.create_analysis(params(kind='interval', start_mmdd='01-03', end_mmdd='01-05'))
    before = _key(research['id'], old, security)
    latest = extend_prices(security, old, date(2023, 1, 5), date(2023, 1, 5))
    assert _key(research['id'], latest, security) != before


def test_company_action_rebase_invalidates_even_outside_window():
    security = seed_security()
    old = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5))
    research = lifecycle.create_analysis(params(kind='interval', start_mmdd='01-03', end_mmdd='01-05'))
    before = _key(research['id'], old, security)
    latest = extend_prices(security, old, date(2023, 6, 1), date(2023, 6, 2))
    with session() as s, s.begin():
        s.get(PriceDataset, latest).manifest = {'verified_splits': {'2023-06-01': '4'}}
    assert _key(research['id'], latest, security) != before


@pytest.mark.parametrize("field,changes_key", [("adj_close", False), ("open", True), ("close", True), ("status", True)])
def test_split_only_fingerprint_uses_actual_financial_fields(field, changes_key):
    security = seed_security()
    old = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5))
    research = lifecycle.create_analysis(params(kind="interval", start_mmdd="01-03", end_mmdd="01-05"))
    before = _key(research["id"], old, security)
    with session() as s, s.begin():
        prior = s.get(PriceDataset, old)
        new = PriceDataset(security_id=security, basis=prior.basis, basis_key=prior.basis_key,
            status="PUBLISHED", manifest=prior.manifest, published_at=now())
        s.add(new)
        s.flush()
        for link in s.scalars(select(DatasetBar).where(DatasetBar.dataset_id == old)):
            bar = s.get(MarketBar, link.bar_id)
            if link.session_date == date(2023, 1, 4):
                fields = {key:getattr(bar, key) for key in
                    ("open", "high", "low", "close", "adj_close", "volume", "status", "reason")}
                fields[field] = "INVALID" if field == "status" else (fields[field] or Decimal(0)) + Decimal("0.00001")
                bar = MarketBar(security_id=security, session_date=link.session_date,
                    provider=bar.provider, source_hash=bar.source_hash, record_hash=uuid4().hex, **fields)
                s.add(bar)
                s.flush()
            s.add(DatasetBar(dataset_id=new.id, session_date=link.session_date, bar_id=bar.id))
        new_id = new.id
    assert (_key(research["id"], new_id, security) != before) is changes_key


@pytest.mark.parametrize('field', ['provider', 'source_hash'])
def test_same_numbers_with_different_price_source_are_not_equivalent(field):
    from iirp.models import SourceObject
    from iirp.storage import save_object

    security = seed_security()
    old = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5))
    request = lifecycle.create_analysis(params(kind='interval', start_mmdd='01-03', end_mmdd='01-05'))
    before = _key(request['id'], old, security)
    source = save_object(b'Distinct synthetic price source; same numeric values')
    with session() as s, s.begin():
        s.add(SourceObject(**source))
        prior = s.get(PriceDataset, old)
        new = PriceDataset(security_id=security, basis=prior.basis, basis_key=prior.basis_key,
            status='PUBLISHED', manifest=prior.manifest, published_at=now())
        s.add(new)
        s.flush()
        for link in s.scalars(select(DatasetBar).where(DatasetBar.dataset_id == old)):
            bar = s.get(MarketBar, link.bar_id)
            if link.session_date == date(2023, 1, 4):
                values = {key: getattr(bar, key) for key in (
                    'open', 'high', 'low', 'close', 'adj_close', 'volume', 'status', 'reason', 'provider', 'source_hash')}
                values[field] = source['sha256'] if field == 'source_hash' else 'synthetic_other_provider'
                bar = MarketBar(security_id=security, session_date=link.session_date,
                    record_hash=uuid4().hex, **values)
                s.add(bar)
                s.flush()
            s.add(DatasetBar(dataset_id=new.id, session_date=link.session_date, bar_id=bar.id))
        identifier = new.id
    assert _key(request['id'], identifier, security) != before
