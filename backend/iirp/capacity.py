"""Shared conservative capacity estimates for UI and backup/restore guards."""

FILE_ALLOCATION_ALLOWANCE = 4096


def temporary_bytes(kind: str, database_bytes: int, payload_bytes: int, object_count: int) -> int:
    """Backup input is the missing pool subset; restore input is the manifest."""
    if min(database_bytes, payload_bytes, object_count) < 0:
        raise ValueError("capacity inputs must be nonnegative")
    if kind == "backup":
        database_copies = 1
    elif kind == "restore":
        database_copies = 2
    else:
        raise ValueError("unknown capacity operation")
    return database_copies * database_bytes + payload_bytes + object_count * FILE_ALLOCATION_ALLOWANCE


def capacity_sufficient(free_bytes: int | None, required_bytes: int | None, reserve_bytes: int) -> bool | None:
    if free_bytes is None or required_bytes is None:
        return None
    return free_bytes >= reserve_bytes + required_bytes
