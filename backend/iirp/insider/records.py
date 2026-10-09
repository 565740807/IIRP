"""One transaction's record and row-level amendment decisions."""


from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from iirp.insider.common import VISIBLE, _group_date, _json
from iirp.insider.views import _event_view, _legacy_labels, _refresh_groups, _with_filing_owners
from iirp.messages import UserError, msg
from iirp.models import (
    AmendmentRelation,
    Filing,
    FilingVersion,
    Issuer,
    Security,
    TransactionEvent,
    now,
)


def entity_history(
    s: Session,
    kind: str,
    id: str,
    start=None,
    end=None,
    recent_count=None,
    date_basis="transaction_date",
    cursor="",
    limit=20,
    session_id="",
) -> dict:
    from iirp.insider.entities import read_entity_history

    return read_entity_history(
        s, kind, id, start, end, recent_count, date_basis, cursor, limit, session_id
    )


def transaction_record(s: Session, id: str) -> dict:
    event = s.get(TransactionEvent, id)
    if event is None:
        raise UserError("insider.transaction_not_saved")
    filing, version = s.get(Filing, event.accession), s.get(FilingVersion, event.version_id)
    relations = list(
        s.scalars(
            select(AmendmentRelation).where(
                or_(
                    AmendmentRelation.amended_event_id == id,
                    AmendmentRelation.original_event_id == id,
                )
            )
        )
    )
    securities = list(s.scalars(select(Security).where(Security.issuer_id == event.issuer_id)))
    return {
        **_with_filing_owners(s, [_event_view(event)])[0],
        "first_seen_at": filing.first_seen_at.isoformat(),
        "source_url": version.data.get("source_url"),
        "source_hash": version.source_hash,
        # Original filing documents; read through /api/v1/sources/{sha256}.
        "source_documents": [
            {"url": url, "sha256": digest}
            for url, digest in (version.data.get("source_objects") or {}).items()
        ],
        "xml_sha256": version.data.get("xml_sha256"),
        "source_visible": filing.visible,
        "filing_status": filing.status,
        "owner_observations": version.data.get("owners", []),
        "amendments": [
            {
                "id": relation.id,
                "action": relation.action,
                "original_event_id": relation.original_event_id,
                "amended_event_id": relation.amended_event_id,
                "evidence": _legacy_labels(relation.evidence),
            }
            for relation in relations
        ],
        "securities": [
            {"id": security.id, "symbol": security.symbol, "status": security.status}
            for security in securities
        ],
        "security_id": None,
        "security_notice": msg("insider.security_notice"),
    }


def resolve_amendment(
    s: Session, relation_id: str, action: str, original_event_id: str | None, evidence
) -> dict:
    """Apply a documented row-level decision, preserving both source observations."""
    aliases = {
        "additional": "ADD",
        "new": "ADD",
        "add": "ADD",
        "replace": "REPLACE",
        "unchanged": "UNCHANGED",
        "no_change": "UNCHANGED",
        "withdraw": "REMOVE",
        "remove": "REMOVE",
    }
    chosen = aliases.get(action.lower(), action.upper())
    if chosen not in {"ADD", "REPLACE", "UNCHANGED", "REMOVE"}:
        raise UserError("insider.amendment.action_invalid")
    if isinstance(evidence, str):
        evidence = {"note": evidence}
    if (
        not isinstance(evidence, dict)
        or not str(evidence.get("note", evidence.get("description", ""))).strip()
    ):
        raise UserError("insider.amendment.evidence_missing")
    relation = s.scalar(
        select(AmendmentRelation).where(AmendmentRelation.id == relation_id).with_for_update()
    )
    if relation is None:
        raise UserError("insider.amendment.not_found")
    if relation.action not in {"UNCONFIRMED", "UNCONFIRMED_DUPLICATE"}:
        if relation.action == chosen and relation.original_event_id == original_event_id:
            return {"id": relation.id, "action": relation.action, "reused": True}
        raise UserError("insider.amendment.already_resolved")
    amended = s.get(TransactionEvent, relation.amended_event_id)
    if amended:
        s.scalar(select(Issuer).where(Issuer.id == amended.issuer_id).with_for_update())
    event_ids = sorted({value for value in [relation.amended_event_id, original_event_id] if value})
    locked = {
        event.id: event
        for event in s.scalars(
            select(TransactionEvent)
            .where(TransactionEvent.id.in_(event_ids))
            .order_by(TransactionEvent.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    amended = locked.get(relation.amended_event_id)
    original = locked.get(original_event_id)
    if not amended or (chosen != "ADD" and not original):
        raise UserError("insider.amendment.original_required")
    if original and (
        original.id == amended.id
        or original.issuer_id != amended.issuer_id
        or original.status not in VISIBLE
        or not set(original.owner_ids) & set(amended.owner_ids)
    ):
        raise UserError("insider.amendment.original_invalid")
    changed = {(amended.issuer_id, _group_date(amended))}
    if original:
        changed.add((original.issuer_id, _group_date(original)))
    if chosen == "ADD":
        amended.status = "CURRENT"
        if relation.action == "UNCONFIRMED_DUPLICATE" and original:
            original.status = "CURRENT"
    elif chosen == "REPLACE":
        original.status, amended.status = "SUPERSEDED", "CURRENT"
        amended.replaces_id = original.id
        # Correct the original disclosure group without counting the amendment
        # as a second economic transaction on its acceptance day.
        amended.data = {
            **amended.data,
            "original_accepted_at": _json(
                original.data.get("original_accepted_at") or original.accepted_at
            ),
            "original_accession": original.data.get("original_accession", original.accession),
        }
    elif chosen == "UNCHANGED":
        amended.status, original.status = "DUPLICATE_CONFIRMED", "CURRENT"
    else:
        amended.status, original.status = "WITHDRAWN", "WITHDRAWN"
    relation.original_event_id = original.id if original else None
    relation.evidence = {
        **relation.evidence,
        "resolution": {
            **evidence,
            "recorded_at": now().isoformat(),
            "original_event_id": original.id if original else None,
            "action": chosen,
        },
    }
    relation.action = chosen
    s.flush()
    _refresh_groups(s, changed)
    return {
        "id": relation.id,
        "action": chosen,
        "amended_event_id": amended.id,
        "original_event_id": relation.original_event_id,
        "reused": False,
    }
