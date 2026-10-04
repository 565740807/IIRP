"""Lossless result storage and a linear, immutable overlap query projection."""

import json
import zlib

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from iirp.analytics.event_overlaps import REPRESENTATION_VERSION

PAYLOAD_MAGIC = b"IIRP-JSON-ZLIB\x00\x01"


def encode_payload(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return PAYLOAD_MAGIC + zlib.compress(raw, level=1)


def decode_payload(value):
    if not value.startswith(PAYLOAD_MAGIC):
        raise ValueError("Unknown analysis payload format")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(value[len(PAYLOAD_MAGIC):])
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Incomplete or trailing analysis payload data")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Analysis payload must be a JSON object")
    return result


class CompressedResultJSON(TypeDecorator):
    """One lossless decode per ORM load; the Python attribute remains a dict."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encode_payload(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decode_payload(value)


def overlap_projection(data):
    observer = data if data.get("kind") == "event_dates" else data.get("date_observation")
    if not observer or observer.get("metadata", {}).get("representation_version") != REPRESENTATION_VERSION:
        return None

    def project(row):
        points = row.get("points") or []
        return {
            "key": row["key"], "label": row["label"], "overlap": row["overlap"],
            "points": [{"date": points[0]["date"]}, {"date": points[-1]["date"]}] if points else [],
        }

    return {
        "metadata": {"representation_version": REPRESENTATION_VERSION},
        "rows": [project(row) for row in observer.get("rows", [])],
        "coverage_rows": [project(row) for row in observer.get("coverage_rows", [])],
    }


# Include the legacy column, the lossless payload and its small query projection.
RESULT_BYTES_SQL = """SELECT coalesce(sum(pg_column_size(data)
    + coalesce(pg_column_size(payload),0)
    + coalesce(pg_column_size(overlap_projection),0)),0) FROM analysis_result"""
