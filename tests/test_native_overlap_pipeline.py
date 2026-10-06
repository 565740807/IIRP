"""Native earnings executes research_compute, publishes, pages and exports."""
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient
from iirp import lifecycle
from iirp.api import app
from iirp.business_models import AnalysisResult, EarningsEvent
from iirp.business_worker import execute_business
from iirp.db import session
from iirp.models import Job
from iirp.operation_pool import OperationPool
from iirp.queue import claim
from sqlalchemy import select
from test_earnings_lifecycle import event
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    seed_prices,
    seed_security,
)
from test_performance_pipeline import params


def native_published(count=13):
    security = seed_security()
    seed_prices(security, date(2023, 1, 1), date(2025, 1, 1), wide=True)
    with session() as s, s.begin():
        for i in range(count):
            event(s, type('Identity', (), {'id': security})(), day=str(date(2024, 6, 10) + timedelta(days=i)), year=2024,
                  quarter=i % 4 + 1, precision='date_only')
    request = lifecycle.create_analysis(params(kind='earnings', historical_years=1,
        current_year=2025, current_fiscal_year=2025, years=[2024]))
    lifecycle.plan_tick()
    job = claim({'research_compute'})
    assert job and job.target['analysis_id'] == request['id']
    pool = OperationPool()
    try:
        execute_business(job, runner=pool)
    finally:
        pool.close()
    with session() as s:
        assert s.get(Job, job.id).status == 'SUCCEEDED'
        result_id = s.scalar(select(AnalysisResult.id).where(AnalysisResult.analysis_id == request['id']))
    return lifecycle.get_analysis(request['id'], result_ids=result_id), job


def test_native_earnings_real_pool_frozen_pages_exports_and_reuse():
    view, job = native_published()
    item = view['results'][0]
    result_id = item['result_id']
    observer = item['data']['date_observation']
    key = observer['rows'][0]['key']
    expected = [r['key'] for r in observer['rows'][1:]]
    assert len(expected) == 12
    reused = lifecycle.create_analysis(params(kind='earnings', historical_years=1,
        current_year=2025, current_fiscal_year=2025, years=[2024]))
    assert reused['results'][0]['data']['date_observation']['rows'] == observer['rows']
    with session() as s:
        copied = s.get(AnalysisResult, reused['results'][0]['result_id'])
        assert copied.inputs['reused_from'] == result_id
        assert copied.overlap_projection['rows'][0]['overlap'] == observer['rows'][0]['overlap']
    url = f"/api/v1/analyses/{view['id']}/results/{result_id}/event-overlaps"
    with TestClient(app) as client:
        before = client.get(url, params={'event_key': key}).json()
        assert before['total'] == 12 and before['summary'] == observer['rows'][0]['overlap']
        assert [r['event_key'] for r in before['items']] == expected
        exports = {}
        for fmt in ('json', 'csv'):
            response = client.get(f"/api/v1/analyses/{view['id']}/export", params={'result_ids':result_id,'format':fmt})
            assert response.status_code == 200
            exports[fmt] = response.content
        assert key in exports['json'].decode() and key in exports['csv'].decode()
        assert 'date_observation' in exports['json'].decode()
        # A real subsequent source revision cannot change the frozen result.
        with session() as s, s.begin():
            original = s.get(EarningsEvent, key)
            original.announced_date = date(2024, 7, 10)
            original.revision += 1
        assert client.get(url, params={'event_key':key}).json() == before
        for fmt in exports:
            response = client.get(f"/api/v1/analyses/{view['id']}/export", params={'result_ids':result_id,'format':fmt})
            assert response.content == exports[fmt]
    assert item['data']['kind'] == 'earnings'
    assert observer['metadata']['representation_version'] == 'event-overlaps-v2'
    assert item['data']['effective_n'] == 0  # Date-only cannot gain precise-time qualification.
    assert observer['rows'][0]['sources']
    assert json.loads(exports['json'])
    assert job.kind == 'research_compute'
