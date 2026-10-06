"""Copy an existing result row without decoding its complete compressed payload."""
from sqlalchemy import insert, literal, select
from sqlalchemy.orm import load_only

from iirp.business_models import AnalysisResult, uid
from iirp.models import now


def result_identity(s, analysis_id, security_id, input_key):
    return s.scalar(select(AnalysisResult).options(load_only(
        AnalysisResult.id, AnalysisResult.analysis_id, AnalysisResult.security_id,
        AnalysisResult.input_key, AnalysisResult.inputs,
    )).where(AnalysisResult.analysis_id == analysis_id,
             AnalysisResult.security_id == security_id, AnalysisResult.input_key == input_key))


def clone_frozen(s, source_id, request, security_id, input_key):
    """Caller holds the reference lock and request publication/control fence.

    Only exact event inputs qualify. SQL copies all three storage columns from
    ONE immutable row, preserving the E2.1 pair/codec and provenance verbatim.
    No serialization, decode, or mutable Python payload is needed to copy it.
    """
    existing = result_identity(s, request.id, security_id, input_key)
    if existing:
        return existing
    table = AnalysisResult.__table__
    identifier, stamp = uid(), now()
    s.execute(insert(table).from_select(
        ['id', 'analysis_id', 'security_id', 'input_key', 'inputs', 'data',
         'payload', 'overlap_projection', 'created_at', 'accessed_at', 'expires_at'],
        select(literal(identifier), literal(request.id), table.c.security_id,
            table.c.input_key, table.c.inputs, table.c.data, table.c.payload,
            table.c.overlap_projection, literal(stamp), literal(stamp), table.c.expires_at)
        .where(table.c.id == source_id, table.c.security_id == security_id,
               table.c.input_key == input_key)))
    return result_identity(s, request.id, security_id, input_key)
