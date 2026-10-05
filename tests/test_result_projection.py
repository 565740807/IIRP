"""Atomic storage, legacy fallback, cache/copy and reversible isolated migration."""

import copy
import json
import zlib
from hashlib import sha256

import pytest
from alembic import command
from alembic.config import Config
from iirp.business_models import AnalysisResult
from iirp.config import ROOT
from iirp.db import session
from iirp.event_overlap_reads import get_overlap_page
from iirp.result_storage import PAYLOAD_MAGIC, RESULT_BYTES_SQL, decode_payload, encode_payload
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_event_overlap_reads import published
from test_lifecycle import clean_lifecycle, lifecycle_database  # noqa: F401


def test_payload_codec_preserves_json_values_and_rejects_corruption():
    value = {"text": "完整来源\n\u0001é😀", "values": [None, True, False, 0, -3, 1.2345678901234567,
                                                10**40, {"decimal": "0.000000000000123456789"}]}
    encoded = encode_payload(value)
    assert encoded.startswith(PAYLOAD_MAGIC)
    assert decode_payload(encoded) == value
    for invalid in (b"unknown" + encoded, encoded[:-1], encoded + b"trailing",
                    encoded[:len(PAYLOAD_MAGIC)] + b"damaged", PAYLOAD_MAGIC + zlib.compress(b"[]")):
        with pytest.raises((ValueError, zlib.error)):
            decode_payload(invalid)


def test_failed_downgrade_rolls_back_prior_conversion_and_schema():
    from sqlalchemy import event

    _, _, view, _ = published(13)
    cfg = Config(str(ROOT / 'alembic.ini'))
    with session() as s, s.begin():
        original = s.get(AnalysisResult, view['result_id'])
        # Corrupt row sorts last, after one valid conversion has executed.
        s.add(AnalysisResult(id='zz-synthetic-corrupt', analysis_id=original.analysis_id,
                             security_id=original.security_id, input_key='corrupt-downgrade',
                             inputs=original.inputs, data=original.data))
    with session() as s, s.begin():
        s.execute(text("UPDATE analysis_result SET payload=:payload WHERE id='zz-synthetic-corrupt'"),
                  {'payload':b'unknown-format'})
    with session() as s:
        head = s.scalar(text('SELECT version_num FROM alembic_version'))
        before = s.execute(text('SELECT id,data,payload,overlap_projection,ctid::text,xmin::text '
                                'FROM analysis_result ORDER BY id')).all()
    from iirp.db import engine

    converted = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.startswith('UPDATE analysis_result SET data=CAST'):
            converted.append(True)

    event.listen(engine(), 'after_cursor_execute', capture)
    try:
        with pytest.raises(ValueError, match='Unknown analysis payload format'):
            command.downgrade(cfg, '0017')
    finally:
        event.remove(engine(), 'after_cursor_execute', capture)
    assert len(converted) == 1
    with session() as s:
        assert s.scalar(text('SELECT version_num FROM alembic_version')) == head
        assert s.execute(text('SELECT id,data,payload,overlap_projection,ctid::text,xmin::text '
                              'FROM analysis_result ORDER BY id')).all() == before
        with pytest.raises(ValueError, match='Unknown analysis payload format'):
            s.get(AnalysisResult, 'zz-synthetic-corrupt')
    with pytest.raises(IntegrityError):
        with session() as s, s.begin():
            s.execute(text('UPDATE analysis_result SET overlap_projection=NULL WHERE id=:id'),
                      {'id': view['result_id']})


def test_failed_partial_jsonb_reconstruction_is_fully_rolled_back():
    from iirp.db import engine
    from sqlalchemy import event

    _, _, view, _ = published(123)
    cfg = Config(str(ROOT / 'alembic.ini'))
    query = text('SELECT data,payload,overlap_projection,ctid::text,xmin::text '
                 'FROM analysis_result WHERE id=:id')
    with session() as s:
        head = s.scalar(text('SELECT version_num FROM alembic_version'))
        before = s.execute(query, {'id': view['result_id']}).one()
    converted = []

    def fail_partway(conn, cursor, statement, parameters, context, many):
        if statement.startswith('UPDATE analysis_result SET data='):
            converted.append(True)
            if len(converted) == 3:
                raise RuntimeError('synthetic failure during partial JSONB reconstruction')

    event.listen(engine(), 'after_cursor_execute', fail_partway)
    try:
        with pytest.raises(RuntimeError, match='partial JSONB reconstruction'):
            command.downgrade(cfg, '0017')
    finally:
        event.remove(engine(), 'after_cursor_execute', fail_partway)
    assert len(converted) == 3
    with session() as s:
        assert s.scalar(text('SELECT version_num FROM alembic_version')) == head
        assert s.execute(query, {'id': view['result_id']}).one() == before
        assert s.get(AnalysisResult, view['result_id']).data == view['data']
    with pytest.raises(IntegrityError):
        with session() as s, s.begin():
            s.execute(text('UPDATE analysis_result SET overlap_projection=NULL WHERE id=:id'),
                      {'id': view['result_id']})


def test_worker_publication_failure_cannot_leave_result_or_projection():
    from iirp.business_worker import execute_business
    from iirp.models import Job
    from iirp.operation_pool import OperationPool
    from iirp.queue import claim
    from sqlalchemy import event
    from test_event_pipeline import create, enqueue

    created, _ = create()
    enqueue(created['analysis_id'])
    job = claim({'event_compute'})

    def fail_after_insert(mapper, connection, target):
        assert target.payload is not None and target.overlap_projection is not None
        raise RuntimeError('synthetic failure inside publication fence')

    event.listen(AnalysisResult, 'after_insert', fail_after_insert)
    pool = OperationPool()
    try:
        execute_business(job, runner=pool)
    finally:
        event.remove(AnalysisResult, 'after_insert', fail_after_insert)
        pool.close()
    with session() as s:
        assert s.get(Job, job.id).status == 'FAILED'
        assert s.scalar(select(AnalysisResult.id)) is None


def test_projection_storage_rollback_copy_and_delete():
    _, _, view, _ = published(123)
    with session() as s:
        original = s.get(AnalysisResult, view['result_id'])
        assert original.legacy_data == {} and original.payload == view['data']
        assert len(original.overlap_projection['rows']) == 123
        assert s.scalar(text(RESULT_BYTES_SQL)) > 0
        values = dict(analysis_id=original.analysis_id, security_id=original.security_id,
                      input_key='rollback-projection', inputs=original.inputs, data=copy.deepcopy(original.data))
    with pytest.raises(RuntimeError, match='synthetic'):
        with session() as s, s.begin():
            s.add(AnalysisResult(**values))
            s.flush()
            raise RuntimeError('synthetic failure after insert, before commit')
    with session() as s:
        assert s.scalar(select(AnalysisResult.id).where(AnalysisResult.input_key == 'rollback-projection')) is None
    with pytest.raises(IntegrityError):
        with session() as s, s.begin():
            s.execute(text('UPDATE analysis_result SET overlap_projection=NULL WHERE id=:id'), {'id':view['result_id']})
    with session() as s, s.begin():
        result = AnalysisResult(**values)
        s.add(result)
        s.flush()
        identifier = result.id
    assert get_overlap_page(view['id'], identifier, 'event-000')['summary'] == view['data']['rows'][0]['overlap']
    with session() as s, s.begin():
        s.delete(s.get(AnalysisResult, identifier))
    with pytest.raises(LookupError):
        get_overlap_page(view['id'], identifier, 'event-000')
    assert get_overlap_page(view['id'], view['result_id'], 'event-000')['total'] == 122


@pytest.mark.slow
def test_large_projection_migration_round_trip_and_old_fallback():
    _, _, view, _ = published(1500)
    analysis_id, result_id = view['id'], view['result_id']

    def content_hash(data):
        return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    expected = content_hash(view['data'])
    del view  # Do not retain multiple decoded 1500-row trees in this fixture.
    cfg = Config(str(ROOT / 'alembic.ini'))
    before = get_overlap_page(analysis_id, result_id, 'event-000')
    command.downgrade(cfg, '0017')
    try:
        with session() as s:
            raw = s.execute(text('SELECT data,ctid::text,xmin::text FROM analysis_result WHERE id=:id'), {'id':result_id}).one()
            assert content_hash(raw.data) == expected
            physical = raw[1:]
            del raw
    finally:
        command.upgrade(cfg, 'head')
    with session() as s:
        after = s.execute(text('SELECT data,ctid::text,xmin::text FROM analysis_result WHERE id=:id'), {'id':result_id}).one()
        assert content_hash(after.data) == expected and after[1:] == physical
        del after  # Upgrade preserves old content and physical row identity.
        result = s.get(AnalysisResult, result_id)
        assert result.payload is None and result.overlap_projection is None
        assert content_hash(result.data) == expected
    assert get_overlap_page(analysis_id, result_id, 'event-000') == before
