"""Distinguish a quoted issuer token from a usable market symbol."""

PLACEHOLDERS = {"NONE", "N/A", "NA", "NULL", "UNKNOWN", "NOT APPLICABLE", "--", "-"}


def normalized_ticker(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    symbol = value.strip().upper()
    return symbol if symbol and symbol not in PLACEHOLDERS else None
