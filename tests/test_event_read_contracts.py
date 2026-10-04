"""HTTP envelope compatibility without a database or provider.

PG/OperationPool cases remain in the integration suites; these assert that the
new response DTO itself neither drops nor normalizes saved historical facts.
"""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from iirp import event_api
from iirp.event_views import (
    EventAnalysisCreated,
    EventAnalysisOutput,
    EventConfirmOutput,
    EventSetOutput,
    EventSetsOutput,
)


def saved_set():
    return {
        'id': 'set', 'title': '合成契约', 'kind': 'custom', 'version': 1,
        'latest_version': 2, 'version_id': 'v1',
        'security': {'id': 'security', 'symbol': 'SYN', 'name': '', 'currency': None,
                     'exchange': None, 'status': 'PENDING', 'calendar': None},
        'created_at': '2026-09-01T00:00:00+00:00', 'updated_at': '2026-10-01T00:00:00+00:00',
        'research_as_of': '2026-09-01', 'event_count': 1, 'selected_count': 1,
        'verified_count': 0, 'revision_note': '原文不规范化',
        'document': {'legacy_schema': 'v0', 'source': {'unrecognized': ['中文', None, '1.00000']}},
        'events': [{'client_event_id': 'e', 'review': {'note': '保留\\n\"完整\"\n备注'}, 'legacy': True}],
        'reviews': [{'note': '核验\n原文', 'future_field': {'a': 'b'}}],
        'raw_text': '{"原文": "保留空格"}', 'content_hash': 'unchanged', 'warnings': [],
        'versions': [{'id': 'v1', 'version': 1, 'created_at': 'original-time', 'revision_note': ''}],
        'analyses': [{'id': 'a', 'batch_id': 'b', 'params': {'legacy': True}, 'created_at': 'original-time'}],
    }


def saved_analysis(payload):
    return {'id': 'a', 'batch_id': 'b', 'params': {'event_set_id': 'set', 'event_version': 1},
            'status': 'PARTIAL', 'requested_action': None, 'created_at': 'original-time',
            'progress': [{'symbol': 'SYN', 'status': 'PARTIAL', 'wait_reason': 'benchmark_missing_prices',
                          'legacy_checkpoint': {'complete': False}}],
            'result_id': 'r', 'result_cutoff': '2026-09-01', 'carried_from': None,
            'data': payload, 'results': [{'id': 'r', 'dataset_id': None, 'created_at': 'original-time'}]}


def test_envelope_roundtrip_preserves_unknown_payload_and_omitted_progress():
    original = saved_set()
    analysis = saved_analysis({'legacy': original['events'], 'sources': original['document']})
    receipt = {'set_id': 'set', 'version': 1, 'version_id': 'v1', 'reused': True}
    created = {'analysis_id': 'a', 'batch_id': 'b', 'reused': True}
    for model, value in [(EventSetOutput, original), (EventSetsOutput, {'items': [original]}),
                         (EventAnalysisOutput, analysis), (EventConfirmOutput, receipt),
                         (EventAnalysisCreated, created)]:
        assert model.model_validate(value).model_dump(mode='json', exclude_unset=True) == value
    # Unknown coverage stays absent, never gains a false zero or complete flag.
    assert 'ready_events' not in EventAnalysisOutput.model_validate(analysis).model_dump(exclude_unset=True)['progress'][0]


@pytest.mark.parametrize('payload', [None, {'metadata': {'calculation_version': 'legacy'},
    'overlapping_event_ids': ['a', 'b'], 'precise_decimal': '0.10000000000000000001',
    'sources': [{'url': 'https://example.test/evidence', 'note': '原始来源\n完整备注'}]},
    {'metadata': {'overlap_representation': 'event-overlaps-v2'},
     'overlap_summary': {'total': 1499, 'preview_event_ids': ['a']}, 'new_unknown_field': [1, None]}])
def test_event_analysis_http_preserves_legacy_and_current_fields(monkeypatch, payload):
    original = saved_analysis(payload)
    monkeypatch.setattr(event_api.event_service, 'get_analysis', lambda *_: deepcopy(original))
    app = FastAPI()
    app.include_router(event_api.router)
    with TestClient(app) as client:
        response = client.get('/api/v1/events/analyses/a?result_id=r')
    assert response.status_code == 200
    assert response.json() == original
    assert EventAnalysisOutput.model_validate(original).model_dump(exclude_unset=True) == original


def test_set_and_confirm_http_preserve_saved_documents_and_optional_fields(monkeypatch):
    original = saved_set()
    summary = {k: v for k, v in original.items() if k not in {
        'document', 'events', 'reviews', 'raw_text', 'content_hash', 'warnings', 'versions', 'analyses'}}
    receipt = {'set_id': 'set', 'version': 1, 'version_id': 'v1', 'reused': True}
    created = {'analysis_id': 'a', 'batch_id': 'b', 'reused': True}
    monkeypatch.setattr(event_api.event_service, 'get_set', lambda *_: deepcopy(original))
    monkeypatch.setattr(event_api.event_service, 'list_sets', lambda *_: {'items': [summary]})
    monkeypatch.setattr(event_api.event_service, 'confirm_import', lambda *_: receipt)
    monkeypatch.setattr(event_api.event_service, 'create_analysis', lambda *_: created)
    app = FastAPI()
    app.include_router(event_api.router)
    with TestClient(app) as client:
        assert client.get('/api/v1/events/sets/set?version=1').json() == original
        assert client.get('/api/v1/events/sets').json() == {'items': [summary]}
        assert client.post('/api/v1/events/confirm', json={
            'preview_id': 'preview', 'request_id': 'request', 'reviews': []}).json() == receipt
        assert client.post('/api/v1/events/sets/set/analyses', json={
            'version': 1, 'request_id': 'request'}).json() == created
    assert app.openapi()['paths']['/api/v1/events/analyses/{analysis_id}']['get']['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/EventAnalysisOutput')
