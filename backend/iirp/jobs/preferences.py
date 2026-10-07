"""Product preferences and automatic-update strategies."""


from sqlalchemy import select

from iirp.db import session
from iirp.jobs.batches import control_batch, defaults, product_preferences
from iirp.messages import NotFoundError
from iirp.models import (
    Batch,
    CollectionStrategy,
    Preferences,
    now,
)


def get_preferences():
    with session() as s:
        p = s.get(Preferences, 1)
        return {
            "values": p.values if p else product_preferences(),
            "version": p.version if p else 1,
        }


def update_preferences(values):
    with session() as s, s.begin():
        defaults(s)
        p = s.get(Preferences, 1, with_for_update=True)
        previous = {**product_preferences(), **p.values}
        p.values = {**previous, **values}
        p.version += 1
        # Fence old automatic demands when their saved boundary is no longer wanted.
        stop_history = not p.values["automatic_history"] or (
            p.values["history_months"] < previous["history_months"]
        )
        ids = (
            list(
                s.scalars(
                    select(Batch.id).where(
                        Batch.kind == "sec_history",
                        Batch.trigger == "automatic",
                        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT", "PARTIAL")),
                    )
                )
            )
            if stop_history
            else []
        )
        policy = s.get(CollectionStrategy, "sec", with_for_update=True)
        if policy.enabled:
            policy.next_run_at = now()
        result = {"values": p.values, "version": p.version}
    for identifier in ids:
        control_batch(identifier, "cancel")
    return result


def get_strategies():
    from iirp.jobs.providers import SEC_USER_AGENT_HINT, sec_configured

    blocked = {} if sec_configured() else {"sec": SEC_USER_AGENT_HINT}
    with session() as s:
        return {
            "items": [
                {
                    **{
                        k: getattr(p, k)
                        for k in ("key", "enabled", "version", "options", "next_run_at", "last_run_at")
                    },
                    "blocked_reason": blocked.get(p.key),
                }
                for p in s.scalars(select(CollectionStrategy).order_by(CollectionStrategy.key))
            ]
        }


def update_strategy(key, enabled):
    with session() as s, s.begin():
        defaults(s)
        p = s.get(CollectionStrategy, key, with_for_update=True)
        if not p:
            raise NotFoundError("preference.not_found")
        p.enabled = enabled
        p.options = {**p.options, "user_controlled": True}
        p.version += 1
        p.next_run_at = now() if enabled else None
        ids = (
            list(
                s.scalars(
                    select(Batch.id).where(
                        Batch.policy_key == key,
                        Batch.trigger == "automatic",
                        Batch.status.in_(("QUEUED", "RUNNING", "RETRY_WAIT")),
                    )
                )
            )
            if not enabled
            else []
        )
    for batch_id in ids:
        control_batch(batch_id, "pause")
    return get_strategies()
