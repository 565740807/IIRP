"""Display settings shared by every browser of this instance.

Saved in ``user_preferences.values["display"]`` next to the other preferences, and
read and written only through ``GET/PUT /api/v1/display``; a PUT changes only the
sections it names.

- ``colors`` chooses how up and down are colored everywhere: a preset (``us`` green
  up / red down, the default; ``colorblind`` blue up / orange down; ``asia`` red up /
  green down) or ``custom`` with two picked colors. The preset palettes themselves live
  in the frontend; the backend stores the choice and the custom colors, which are kept
  when another preset is chosen so switching back restores them.
- ``market_strip`` lists the home-strip instruments to show (``iirp.market.instruments``);
  without a saved choice, the catalog's defaults.
"""

import re

from iirp.db import session
from iirp.market import instruments
from iirp.messages import UserError, msg
from iirp.models import Preferences

KEY = "display"
PRESETS = ("us", "colorblind", "asia", "custom")
HEX = re.compile(r"#[0-9a-f]{6}")
DEFAULT_COLORS = {"preset": "us", "up": None, "down": None}


def _colors(saved):
    colors = {**DEFAULT_COLORS, **(saved or {})}
    if colors["preset"] not in PRESETS:
        colors["preset"] = DEFAULT_COLORS["preset"]
    return colors


def _strip(saved):
    symbols = (saved or {}).get("symbols")
    return instruments.ordered(symbols) if isinstance(symbols, list) else list(instruments.DEFAULT_SYMBOLS)


def _saved(row):
    return ((row.values or {}).get(KEY) or {}) if row else {}


def _state(row):
    saved = _saved(row)
    return {
        "colors": _colors(saved.get("colors")),
        "market_strip": {"symbols": _strip(saved.get("market_strip"))},
        "instruments": [
            {"symbol": item.symbol, "name": msg(item.name), "kind": item.kind, "default": item.default,
             "off_hours_only": item.off_hours_only}
            for item in instruments.INSTRUMENTS
        ],
        "version": row.version if row else 1,
    }


def strip_selection(s):
    """The home-strip instruments chosen for this instance, in strip order."""
    return _strip(_saved(s.get(Preferences, 1)).get("market_strip"))


def get_display():
    with session() as s:
        return _state(s.get(Preferences, 1))


def _checked_colors(colors):
    colors = dict(colors)
    for side in ("up", "down"):
        if colors.get(side) is not None:
            colors[side] = colors[side].lower()
            if not HEX.fullmatch(colors[side]):
                raise UserError("display.color_invalid", value=colors[side])
    if colors["preset"] == "custom" and not (colors.get("up") and colors.get("down")):
        raise UserError("display.custom_colors_required")
    if colors["preset"] == "custom" and colors["up"] == colors["down"]:
        raise UserError("display.colors_identical")
    return colors


def _checked_strip(strip):
    symbols = list(strip.get("symbols") or [])
    unknown = sorted(set(symbols) - set(instruments.BY_SYMBOL))
    if unknown:
        raise UserError("display.instrument_unknown", symbols=unknown)
    if not symbols:
        raise UserError("display.strip_empty")
    return {"symbols": instruments.ordered(symbols)}


def save_display(values):
    """Validate and save the sections given (colors, market_strip); returns the saved state."""
    changes = {}
    if values.get("colors") is not None:
        changes["colors"] = _checked_colors(values["colors"])
    if values.get("market_strip") is not None:
        changes["market_strip"] = _checked_strip(values["market_strip"])
    from iirp.jobs.batches import defaults

    with session() as s, s.begin():
        defaults(s)
        row = s.get(Preferences, 1, with_for_update=True)
        row.values = {**row.values, KEY: {**_saved(row), **changes}}
        row.version += 1
        s.flush()
        return _state(row)
