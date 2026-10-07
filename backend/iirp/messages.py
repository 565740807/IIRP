"""User-facing text as message codes plus parameters.

The backend does not return finished sentences in any one language. A message is
``{"code": "...", "params": {...}}`` and the frontend renders it with
``frontend/src/locales/<language>/translation.json`` (i18next keys, ``{{name}}``
placeholders). Messages travel through fields that were plain text before — database
columns, exception text, subprocess results, JSON responses — so ``msg`` returns the
compact JSON of that object as a string. HTTP error responses carry the decoded object
in ``detail``. Text written before messages existed passes through as-is.
"""

import json
from typing import Any


def msg(code: str, **params: Any) -> str:
    """Encode one message; parameters are data (numbers, dates, symbols), never prose."""
    return json.dumps({"code": code, "params": params}, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def decode(value: Any) -> dict[str, Any] | None:
    """Return the message object encoded in ``value``, or None for plain text."""
    if not isinstance(value, str) or not value.startswith('{"code":'):
        return None
    try:
        data = json.loads(value)
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("code"), str) else None


class UserError(ValueError):
    """Rejected input or state the user can act on; ``str()`` is the encoded message."""

    def __init__(self, code: str, **params: Any):
        super().__init__(msg(code, **params))
        self.code, self.params = code, params


class NotFoundError(LookupError):
    """A requested record does not exist; ``str()`` is the encoded message."""

    def __init__(self, code: str, **params: Any):
        super().__init__(msg(code, **params))
        self.code, self.params = code, params
