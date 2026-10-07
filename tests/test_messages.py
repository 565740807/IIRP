"""Every message code the backend emits has a translation, and messages round-trip."""

import ast
import json
import re
from pathlib import Path

from iirp.messages import NotFoundError, UserError, decode, msg

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "frontend/src/locales"
EMITTERS = {"msg", "UserError", "NotFoundError"}


def _codes(node):
    """Literal codes (exact) and prefixes (built with + or an f-string) of one argument."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}, set()
    if isinstance(node, ast.IfExp):
        exact, prefixes = _codes(node.body)
        other = _codes(node.orelse)
        return exact | other[0], prefixes | other[1]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        exact, prefixes = _codes(node.left)
        return set(), prefixes | exact
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return set(), {node.values[0].value}
    return set(), set()


def emitted_codes():
    exact, prefixes = set(), set()
    for path in (ROOT / "backend/iirp").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if (isinstance(node, ast.Call) and node.args
                    and getattr(node.func, "id", getattr(node.func, "attr", None)) in EMITTERS):
                found = _codes(node.args[0])
                exact |= found[0]
                prefixes |= found[1]
    return exact, prefixes


def test_every_emitted_code_is_translated():
    exact, prefixes = emitted_codes()
    assert len(exact) > 300
    for language in [p.name for p in LOCALES.iterdir()]:
        keys = json.loads((LOCALES / language / "translation.json").read_text())
        assert not sorted(code for code in exact if code not in keys), language
        assert not sorted(p for p in prefixes if not any(key.startswith(p) for key in keys)), language


def test_translations_use_known_placeholders():
    keys = json.loads((LOCALES / "zh/translation.json").read_text())
    for key, text in keys.items():
        for placeholder in re.findall(r"\{\{([^}]*)\}\}", text):
            name, _, formatter = (part.strip() for part in placeholder.partition(","))
            assert re.fullmatch(r"[a-z_]+", name), key
            assert formatter in {"", "number", "list", "items"}, key


def test_backend_returns_no_chinese_sentences():
    allowed = {"events/prompts.py"}  # user-maintained prompt templates in both languages
    chinese = re.compile(r"[一-鿿]")
    for path in (ROOT / "backend/iirp").rglob("*.py"):
        if path.relative_to(ROOT / "backend/iirp").as_posix() in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and chinese.search(node.value):
                # Old labels read back from stored rows and accepted filter aliases are input.
                assert path.name in {"views.py", "feed.py"}, (path, node.value)


def test_message_round_trip():
    encoded = msg("market.bar.dated", day="2026-01-02", reason=msg("market.bar.jump"))
    assert decode(encoded) == {"code": "market.bar.dated",
                               "params": {"day": "2026-01-02", "reason": msg("market.bar.jump")}}
    assert decode("plain text from an older row") is None
    assert decode(str(UserError("job.queue_full", max=50))) == {"code": "job.queue_full", "params": {"max": 50}}
    assert isinstance(UserError("x"), ValueError) and isinstance(NotFoundError("x"), LookupError)
