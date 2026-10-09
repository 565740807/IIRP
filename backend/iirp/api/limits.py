"""Request size limits for state-changing calls (checked by the API middleware)."""

# Pasted event JSON is at most 512 KB of text; JSON escaping can grow it a few times.
EVENT_REQUEST_BYTES = 4 * 1024**2
SMALL_REQUEST_BYTES = 16 * 1024


def request_limit(path: str) -> int:
    if path.startswith("/api/v1/events/"):
        return EVENT_REQUEST_BYTES
    return SMALL_REQUEST_BYTES
