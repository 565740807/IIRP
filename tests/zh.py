"""Render backend messages with the Chinese translation file, as the frontend does."""

import json
import re
from pathlib import Path

from iirp.messages import decode

TRANSLATIONS = json.loads(
    (Path(__file__).resolve().parents[1] / "frontend/src/locales/zh/translation.json").read_text()
)
SEPARATORS = {"list": TRANSLATIONS["format.list_separator"], "items": TRANSLATIONS["format.item_separator"]}


def zh(value):
    """Text of a message (encoded string or object); other values pass through as text."""
    message = value if isinstance(value, dict) and "code" in value else decode(value)
    if message is None:
        return value if isinstance(value, str) else str(value)
    params = message.get("params") or {}

    def placeholder(match):
        name, _, formatter = (part.strip() for part in match.group(1).partition(","))
        child = params.get(name)
        if isinstance(child, list):
            return SEPARATORS[formatter].join(zh(item) for item in child)
        if formatter == "number":
            return f"{child:,}"
        return zh(child)

    return re.sub(r"\{\{([^}]*)\}\}", placeholder, TRANSLATIONS[message["code"]])
