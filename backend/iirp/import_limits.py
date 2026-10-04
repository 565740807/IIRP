"""Shared event-import capacity; Unicode text and JSON transport are different units."""

EVENT_TEXT_CHARACTERS = 4_000_000
EVENT_TEXT_UTF8_BYTES = 16_000_000
# A Unicode scalar may occupy four UTF-8 bytes or twelve bytes as a JSON
# surrogate-pair escape. Reserve envelope space as well as the full text limit.
EVENT_REQUEST_BYTES = 48 * 1024**2
SMALL_REQUEST_BYTES = 16 * 1024


def request_limit(path: str) -> int:
    if path in {"/api/v1/events/preview", "/api/v1/events/confirm"}:
        return EVENT_REQUEST_BYTES
    if path == "/api/v1/imports/preview":
        return 6 * 1024**2
    return SMALL_REQUEST_BYTES


def validate_event_text(value: str) -> str:
    if len(value) > EVENT_TEXT_CHARACTERS:
        raise ValueError(f"资料共 {len(value):,} 字符，最多支持 {EVENT_TEXT_CHARACTERS:,} 字符；原文已保留。请检查是否重复粘贴整份资料。")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("资料包含无效 Unicode 字符，请检查原始 JSON 的字符转义。") from None
    if size > EVENT_TEXT_UTF8_BYTES:
        raise ValueError(f"资料 UTF-8 大小 {size:,} 字节，最多支持 {EVENT_TEXT_UTF8_BYTES:,} 字节；请检查重复内容。")
    return value
