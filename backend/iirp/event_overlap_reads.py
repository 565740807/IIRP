"""Bounded SQL projection from one immutable result, including legacy relations."""

import base64
import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text

from iirp.analytics.event_overlaps import ORDERING, PREVIEW_LIMIT, REPRESENTATION_VERSION
from iirp.db import session


class EventOverlapSummary(BaseModel):
    total: int = Field(ge=0)
    preview_event_ids: list[str] = Field(max_length=PREVIEW_LIMIT)
    preview_limit: Literal[10] = PREVIEW_LIMIT
    truncated: bool
    ordering: Literal["frozen_row_order"] = ORDERING


class EventOverlapItem(BaseModel):
    event_key: str
    label: str
    start_date: str | None
    end_date: str | None


class EventOverlapPage(BaseModel):
    result_id: str
    event_key: str
    representation_version: Literal["event-overlaps-v2", "legacy-full-list-v1"]
    summary: EventOverlapSummary
    total: int
    offset: int
    limit: int
    items: list[EventOverlapItem]
    next_cursor: str | None


def _cursor(result_id, event_key, offset):
    return base64.urlsafe_b64encode(
        json.dumps(
            [result_id, sha256(event_key.encode()).hexdigest(), offset],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).decode()


def _offset(cursor, result_id, event_key):
    if cursor is None:
        return 0
    try:
        owner, key, offset = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            owner != result_id
            or key != sha256(event_key.encode()).hexdigest()
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError
        return offset
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("重叠详情游标无效或不属于当前结果及事件") from exc


# No ORM result load: large prose, prices, and old pair arrays stay in PostgreSQL.
# New relations use only saved points, never live event or price tables.
_SOURCE = """
WITH document AS MATERIALIZED (
 SELECT CASE WHEN overlap_projection IS NOT NULL THEN overlap_projection
             WHEN data->>'kind' = 'event_dates' THEN data
             ELSE data->'date_observation' END AS data
 FROM analysis_result WHERE id=:result_id AND analysis_id=:analysis_id
), rows AS MATERIALIZED (
 SELECT item, ordinal FROM document,
 jsonb_array_elements(data->'rows') WITH ORDINALITY AS r(item, ordinal)
), chosen AS MATERIALIZED (
 SELECT item FROM rows WHERE item->>'key'=:event_key
 UNION ALL
 SELECT item FROM document, jsonb_array_elements(COALESCE(data->'coverage_rows','[]'::jsonb)) item
 WHERE item->>'key'=:event_key
)
"""


def get_overlap_page(analysis_id, result_id, event_key, cursor=None, limit=50):
    if not 1 <= limit <= 100:
        raise ValueError("重叠详情每页须为 1—100 条")
    offset = _offset(cursor, result_id, event_key)
    params = dict(
        analysis_id=analysis_id,
        result_id=result_id,
        event_key=event_key,
        offset=offset,
        limit=limit,
    )
    with session() as s:
        owner = s.execute(
            text("SELECT id FROM analysis_result WHERE id=:result_id AND analysis_id=:analysis_id"),
            params,
        ).first()
        if owner is None:
            raise LookupError("冻结结果不存在或不属于当前分析")
        selected = (
            s.execute(
                text(
                    _SOURCE
                    + """
 SELECT item->'overlap' AS summary,
 COALESCE(jsonb_array_length(item->'overlapping_event_ids'),0) AS legacy_total,
 (SELECT jsonb_agg(value ORDER BY ordinal) FROM
   jsonb_array_elements(COALESCE(item->'overlapping_event_ids','[]'::jsonb))
   WITH ORDINALITY AS p(value, ordinal) WHERE ordinal<=10) AS legacy_preview,
 document.data->'metadata'->>'representation_version' AS version
 FROM chosen CROSS JOIN document
 """
                ),
                params,
            )
            .mappings()
            .first()
        )
        if selected is None:
            raise LookupError("事件不存在于此冻结结果")
        version = selected["version"]
        if version == REPRESENTATION_VERSION:
            summary = EventOverlapSummary.model_validate(selected["summary"]).model_dump()
            predicate = """r.item->>'key' <> :event_key
              AND r.item->'points'->0->>'date' <= c.item->'points'->-1->>'date'
              AND r.item->'points'->-1->>'date' >= c.item->'points'->0->>'date'"""
            query = (
                _SOURCE
                + f"""
 SELECT r.item->>'key' AS event_key, r.item->>'label' AS label,
 r.item->'points'->0->>'date' AS start_date, r.item->'points'->-1->>'date' AS end_date
 FROM rows r CROSS JOIN chosen c WHERE {predicate}
 ORDER BY r.ordinal LIMIT :limit OFFSET :offset
 """
            )
        elif version is None:
            version = "legacy-full-list-v1"
            summary = dict(
                total=selected["legacy_total"],
                preview_event_ids=selected["legacy_preview"] or [],
                preview_limit=PREVIEW_LIMIT,
                truncated=selected["legacy_total"] > PREVIEW_LIMIT,
                ordering=ORDERING,
            )
            # Legacy stored lists are authoritative; do not infer old relations.
            query = (
                _SOURCE
                + """
 SELECT p.value AS event_key, COALESCE(r.item->>'label',p.value) AS label,
 r.item->'points'->0->>'date' AS start_date, r.item->'points'->-1->>'date' AS end_date
 FROM chosen c CROSS JOIN LATERAL
 jsonb_array_elements_text(COALESCE(c.item->'overlapping_event_ids','[]'::jsonb))
 WITH ORDINALITY AS p(value, ordinal)
 LEFT JOIN rows r ON r.item->>'key'=p.value
 WHERE p.ordinal>:offset AND p.ordinal<=:offset+:limit ORDER BY p.ordinal
 """
            )
        else:
            raise ValueError("不支持的冻结重叠表示版本")
        total = summary["total"]
        if offset > total or (offset == total and offset != 0):
            raise ValueError("重叠详情游标超出范围")
        items = [dict(row) for row in s.execute(text(query), params).mappings()]
        if len(items) != min(limit, total - offset):
            raise ValueError("冻结重叠关系与保存的计数不一致")
    following = offset + len(items)
    return dict(
        result_id=result_id,
        event_key=event_key,
        representation_version=version,
        summary=summary,
        total=total,
        offset=offset,
        limit=limit,
        items=items,
        next_cursor=_cursor(result_id, event_key, following) if following < total else None,
    )
