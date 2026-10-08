"""Fail when frontend source has interface text outside the translation files.

All text a user reads comes from ``frontend/src/locales/<language>/translation.json``
through i18next. This flags, outside comments, the generated API types and the
locale files: any Chinese character, JSX text with letters, user-facing attributes
(placeholder, title, alt, aria-label) with words, and string literals that read
like an English sentence. A line can opt out with ``// ui-text-ok: <reason>``.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend/src"
SKIP = {SOURCE / "generated", SOURCE / "locales"}
# Language names are shown in their own language on purpose.
ALLOWED = {"English", "中文"}

CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]+")
STRING = re.compile(r"""(["'`])((?:\\.|(?!\1)[^\\\n])*)\1""")
ATTRIBUTE = re.compile(r"""\b(placeholder|title|alt|aria-label|label)=(["'])([^"']*)\2""")
JSX_TEXT = re.compile(r"(?<![=-])>([^<>{}`;=()]*[A-Za-z]{2,}[^<>{}`;=()]*)</?[A-Za-z{]")
# Two or more words, at least one in lower case (so ticker lists like "MSFT, AAPL" pass).
WORDS = re.compile(r"(?=[^\n]*[a-z]{2})[A-Za-z]{2,}(?:[\s,.:;!?'’-]+[A-Za-z]{2,})+")
SENTENCE = re.compile(r"^[A-Z][a-z]+(?:[ ,]+[A-Za-z'’]+)+[.!?…:]?$")
OPT_OUT = "ui-text-ok:"


def strip_comments(text):
    """Blank out comments, keeping line numbers and string contents (e.g. URLs)."""
    out, i, n = [], 0, len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
        elif c in "\"'`":
            quote = c
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            comment = text[i:end]
            out.append(comment if OPT_OUT in comment else " " * len(comment))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        elif c == "/" and re.search(r"(^|[(,=:\[!&|?{};]|return)\s*$", text[max(0, i - 20):i]):
            # A regular expression literal: its quotes and slashes are not strings.
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 1
                elif text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            out.append(" " * (j + 1 - i))
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def problems(path):
    text = strip_comments(path.read_text())
    for number, line in enumerate(text.splitlines(), 1):
        if OPT_OUT in line:
            continue
        found = [match for match in CJK.findall(line) if match not in ALLOWED]
        if path.suffix == ".tsx":
            found += [m.group(3) for m in ATTRIBUTE.finditer(line) if WORDS.search(m.group(3))]
            found += [m.group(1).strip() for m in JSX_TEXT.finditer(line)
                      if m.group(1).strip() not in ALLOWED and WORDS.search(m.group(1))]
        found += [m.group(2) for m in STRING.finditer(line) if SENTENCE.match(m.group(2))]
        for value in found:
            yield number, value


def main():
    failures = []
    for path in sorted(SOURCE.rglob("*.ts*")):
        if any(path.is_relative_to(skip) for skip in SKIP):
            continue
        for number, value in problems(path):
            failures.append(f"{path.relative_to(ROOT)}:{number}: {value!r}")
    if failures:
        print("Interface text outside frontend/src/locales (use t(...) instead):")
        print("\n".join(failures))
        return 1
    print("No hard-coded interface text in frontend/src")
    return 0


if __name__ == "__main__":
    sys.exit(main())
