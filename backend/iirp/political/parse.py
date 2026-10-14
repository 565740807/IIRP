"""Conservative parsing of public transaction disclosures; no external lookups.

PDF layout/text extraction belongs to pdfplumber and rendering to PDFium. Scanned
or damaged text layers are read by Tesseract. Source wording is retained for every
row, and OCR output is always marked for review regardless of OCR confidence.
"""

import io
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

PARSER_VERSION = "political-v4"
_DATE = r"\b\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})\b"
_AMOUNT = re.compile(
    r"\$?\s*([\d,]+)\s*[-–—]\s*\$?\s*([\d,]+)(?![\d/])"
)
_OPEN_AMOUNT = re.compile(r"(?:over|more than|greater than)\s*\$\s*([\d,]+)", re.I)
_DIRECTION = re.compile(
    r"\b(S(?:ale)?\s*\(Partial\)|S(?:ale)?\s*\(Full\)|Partial\s+Sale|Purchase|Sale|"
    r"Exchange|Bought|Sold|P|S|E)(?=\s|$)", re.I
)
_OWNER = {"sp": "spouse", "spouse": "spouse", "self": "self", "s": "self",
          "jt": "joint", "joint": "joint", "dc": "dependent",
          "dependent child": "dependent", "child": "dependent"}
_ASSET_CODES = {"ST": "stock", "OP": "option", "B": "bond", "CB": "bond",
                "MB": "bond", "GS": "bond", "MF": "fund", "EF": "fund"}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\x00", "")).strip()


def non_transaction_text(value: str | None, *, asset: bool = False) -> bool:
    """Recognize form furniture, not missing trade fields.

    OGE's receipt stamp carries a date and can sit inside the detected grid.
    Dates/row numbers alone therefore cannot establish that a band is a trade.
    Match specific boilerplate only; a genuine asset with an unreadable date,
    direction or amount remains a review row, as does an OGE Energy stock.
    """
    text = _clean(value)
    # A damaged asset can contain only a row number or maturity year. Its full
    # source row may still carry a real amount/date, so do not drop it as a page number.
    if asset and re.fullmatch(r"\d+(?:\s*(?:of|/)\s*\d+)?", text, re.I):
        return False
    return bool(re.fullmatch(
        r"(?:OGE\s+Received\b.*|(?:U\.?S\.?\s+)?Office of Government Ethics\b.*|"
        r"/s/\s+.*\[electronically signed\b.*|"
        r"(?:Filer paid late fee|Data Revised|Per agency reviewer)\b.*|"
        r"Transactions\s+\d+\s+On\b.*|"
        r"(?:\d+\s+)?Received\s+Over\s+\d+\b.*Days\s+Ago\b.*|"
        r"(?:Note|Noto|N0te)\s*:.*(?:form|account|names|information)\b.*|"
        r"(?:form\.\s*)?(?:Do|Oo) not\b.*(?:account|names|information)\b.*|"
        r"OGE\s+Form\s+278\s*[-–]?\s*T\b.*|"
        r"Periodic Transaction Report(?:\s*\([^)]*\))?|"
        r"Page\s+\d+(?:\s+(?:of|/)\s*\d+)?|\d+\s*(?:of|/)\s*\d+|\d+|"
        r"(?:Name of (?:Individual|Filer)|Filer['’]s Name|Asset(?: Name)?|"
        r"Description|Transaction Type|Type of Transaction|Transaction Date|"
        r"Date of Transaction|Notification Date|Amount(?: of Transaction)?|Owner)|"
        r"I CERTIFY\b.*|\*?\s*For the (?:complete|full)\b.*)",
        text, re.I))


def _date(value: str) -> date | None:
    value = _clean(value)
    match = re.search(_DATE, value)
    if not match:
        return None
    try:
        # Government reports in this collection are contemporary; ambiguous
        # two-digit years are accepted only in the documented 2000–2068 window.
        text = match.group().replace("-", "/")
        parsed = datetime.strptime(text, "%m/%d/%Y" if len(text.split("/")[-1]) == 4
                                   else "%m/%d/%y").date()
        return parsed if 2000 <= parsed.year <= 2068 else None
    except ValueError:
        return None


def _money(value: str) -> tuple[Decimal | None, Decimal | None]:
    match = _AMOUNT.search(value)
    if match:
        low, high = (Decimal(part.replace(",", "")) for part in match.groups())
        if 0 <= low <= high:
            return low, high
    match = _OPEN_AMOUNT.search(value)
    if match:
        return Decimal(match.group(1).replace(",", "")) + 1, None
    exact = re.fullmatch(r"\s*\$\s*([\d,]+(?:\.\d{1,2})?)\s*", value)
    if exact:
        amount = Decimal(exact[1].replace(",", ""))
        return amount, amount
    return None, None


def _direction(value: str) -> str:
    normalized = re.sub(r"[^a-z]", "", value.lower())
    if normalized in {"p", "purchase", "bought", "buy"}:
        return "purchase"
    if normalized in {"s", "sale", "salefull", "sfull", "sold", "sell"}:
        return "sale"
    if normalized in {"salepartial", "spartial", "partialsale"}:
        return "partial_sale"
    if normalized in {"e", "exchange"}:
        return "exchange"
    return "unknown"


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _ocr_direction(value: str) -> str:
    """Accept only a unique nearby fixed form option; retain ambiguous cells."""
    exact = _direction(value)
    if exact != "unknown" and (len(re.sub(r"[^a-z]", "", value.lower())) >= 3 or
                               re.fullmatch(r"[PSEpse]", value.strip())):
        return exact
    normalized = re.sub(r"[^a-z0-9]", "", value.lower()).replace("1", "l").replace("0", "o")
    options = {"purchase": "purchase", "sale": "sale", "partialsale": "partial_sale",
               "salepartial": "partial_sale", "salefull": "sale", "exchange": "exchange"}
    distances = [( _edit_distance(normalized, label), result, label)
                 for label, result in options.items()]
    best = min(distance for distance, _, _ in distances)
    winners = [(result, label) for distance, result, label in distances if distance == best]
    if len({result for result, _ in winners}) != 1:
        return "unknown"
    result, label = winners[0]
    limit = 1 if len(label) <= 4 else 2
    return result if best <= limit and len(normalized) >= len(label) - limit else "unknown"


def _row(fields: dict[str, str], raw: str, *, ocr: bool, number: int) -> dict[str, Any]:
    asset = _clean(fields.get("asset"))
    kind = _clean(fields.get("asset_type"))
    code = re.search(r"\[([A-Z]{1,3})\]", asset)
    asset_type = _ASSET_CODES.get(code.group(1), "unknown") if code else "unknown"
    description = f"{asset} {kind}"
    contract = f"{asset} {fields.get('description', '')}"
    # Official bracket codes outrank words in issuer and fund names. A bond
    # ETF is a fund, and an equity symbol may equal a form's asset-type code.
    if asset_type == "unknown":
        evidence = kind or description
        if re.search(r"\b(?:option|call|put)\b", evidence, re.I):
            asset_type = "option"
        elif re.search(r"\b(?:mutual fund|exchange.traded fund|ETF|fund)\b", evidence, re.I):
            asset_type = "fund"
        elif re.search(r"\b(?:bond|municipal|muni|treasury|debenture|notes?)\b", evidence, re.I):
            asset_type = "bond"
        elif re.search(r"\d+(?:\.\d+)?\s*%", evidence):
            asset_type = "bond"
        elif re.search(r"\b(?:stock|common|equity|CMN)\b", evidence, re.I):
            asset_type = "stock"
    ticker = _clean(fields.get("ticker")) or None
    if not ticker:
        tickers = re.findall(r"\(([A-Z][A-Z0-9.\-]{0,14})\)", asset)
        excluded = {"CUSIP"} | ({"USD"} if asset_type == "unknown" else set())
        tickers = list(dict.fromkeys(item for item in tickers if item not in excluded))
        ticker = tickers[0] if len(tickers) == 1 else None
    if ticker and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", ticker):
        ticker = None
    # A bond's identifier or CUSIP is never a stock symbol.
    if asset_type == "bond":
        ticker = None
    owner = _OWNER.get(_clean(fields.get("owner")).lower(), "unknown")
    low, high = _money(fields.get("amount", ""))
    option_type = None
    if asset_type == "option":
        options = re.findall(r"\b(call|put)\b", contract, re.I)
        if len(set(item.lower() for item in options)) == 1:
            option_type = options[0].lower()
    strike = re.search(r"strike(?:\s+price)?\s*(?:of|:|=)?\s*\$?"
                       r"([\d,]+(?:\.\d+)?)", contract, re.I)
    if not strike:
        strike = re.search(r"\$([\d,]+(?:\.\d+)?)\s+strike", contract, re.I)
    expiry = re.search(r"(?:expir(?:es|ing|ation|y)?(?:\s+date)?\s*(?:of|:|=)?\s*)("
                       + _DATE + r")", contract, re.I)
    result = {
        "row_number": number, "owner": owner, "asset_name": asset,
        "ticker": ticker, "ticker_origin": "reported" if ticker else None,
        "asset_type": asset_type, "option_type": option_type,
        "strike_price": Decimal(strike.group(1).replace(",", ""))
        if strike and asset_type == "option" else None,
        "expiration_date": _date(expiry.group(1)) if expiry else None,
        "transaction_type": (_ocr_direction if ocr else _direction)(fields.get("direction", "")),
        "transaction_date": _date(fields.get("date", "")),
        "notification_date": _date(fields.get("notification", "")),
        "amount_low": low, "amount_high": high, "raw_row": raw,
    }
    warnings = []
    if ocr:
        warnings.append("ocr")
    for field in ("asset_name", "transaction_date", "amount_low"):
        if result[field] is None or result[field] == "":
            warnings.append("missing_" + field)
    if result["transaction_type"] == "unknown":
        warnings.append("unknown_transaction_type")
    if owner == "unknown":
        warnings.append("unknown_owner")
    if asset_type == "unknown":
        warnings.append("unknown_asset_type")
    if asset_type == "option" and any(result[field] is None for field in
                                     ("option_type", "strike_price", "expiration_date")):
        warnings.append("incomplete_option")
    result.update(confidence="review" if warnings else "parsed", warnings=warnings)
    return result


def _header(value: str) -> str | None:
    value = _clean(value).lower()
    if "notification" in value:
        return "notification"
    if "owner" in value:
        return "owner"
    if "ticker" in value or "symbol" in value:
        return "ticker"
    if "asset type" in value:
        return "asset_type"
    if "asset" in value or "description" in value or "name of" in value:
        return "asset"
    if "amount" in value:
        return "amount"
    if "type" in value and ("transaction" in value or value == "type"):
        return "direction"
    if "date" in value and "fil" not in value and "report" not in value:
        return "date"
    return None


def _table_rows(table: list[list[str | None]], *, ocr: bool = False) -> list[dict[str, Any]]:
    columns: dict[int, str] = {}
    rows = []
    for cells in table:
        candidate = {idx: key for idx, cell in enumerate(cells) if (key := _header(cell or ""))}
        if "asset" in candidate.values() and "amount" in candidate.values():
            columns = candidate
            continue
        if not columns or not any(cells):
            continue
        fields = {key: _clean(cells[idx]) for idx, key in columns.items() if idx < len(cells)}
        if not fields.get("asset") or non_transaction_text(fields["asset"], asset=True):
            continue
        # Explicit rows with unreadable dates/directions survive as review rows.
        if fields.get("date") or fields.get("direction") or fields.get("amount"):
            rows.append(_row(fields, " | ".join(cell or "" for cell in cells),
                             ocr=ocr, number=len(rows) + 1))
    return rows


def _text_rows(text: str, *, source: str, ocr: bool) -> list[dict[str, Any]]:
    """Parse date/direction anchored lines, preserving wrapped asset descriptions."""
    rows: list[dict[str, Any]] = []
    pending = []
    for line in text.splitlines():
        line = _clean(line)
        if not line or non_transaction_text(line):
            continue
        dates = list(re.finditer(_DATE, line))
        amounts = list(_AMOUNT.finditer(line))
        direction = list(_DIRECTION.finditer(line))
        # Ignore report metadata; transaction candidates require a direction and
        # either a date or an amount. A malformed candidate is still reviewable.
        if not direction or not (dates or amounts or _OPEN_AMOUNT.search(line)):
            if rows and not dates and not amounts and re.search(r"\b(?:DESCRIPTION|COMMENT)", line):
                rows[-1]["raw_row"] += "\n" + line
            elif re.search(r"\([A-Z][A-Z0-9.\-]+\)|\[ST\]|\[OP\]", line):
                pending.append(line)
            continue
        cut = min([match.start() for match in dates + amounts] or [len(line)])
        valid = [match for match in direction if match.start() <= cut]
        if not valid:
            valid = direction
        chosen = valid[-1]
        asset = line[:chosen.start()].strip()
        # OGE can place transaction date before the asset name.
        if source == "oge" and dates and dates[0].start() < chosen.start():
            asset = re.sub(_DATE, "", asset).strip()
        asset = re.sub(r"^\d+[.)]?\s+", "", asset)
        owner_match = re.match(r"^(SP|JT|DC|Self|Spouse|Joint|Dependent Child)\b\s*", asset, re.I)
        owner = owner_match.group(1) if owner_match else ""
        if owner_match:
            asset = asset[owner_match.end():]
        if pending:
            asset = " ".join(pending + [asset])
            pending = []
        fields = {"asset": asset, "owner": owner, "direction": chosen.group(),
                  "date": dates[0].group() if dates else "",
                  "notification": dates[1].group() if len(dates) > 1 and source == "house" else "",
                  "amount": line[amounts[-1].start():] if amounts else line}
        rows.append(_row(fields, line, ocr=ocr, number=len(rows) + 1))
    return rows


def inspect_pdf(payload: bytes) -> int:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(payload)) as document:
        return len(document.pages)


def _lines(words: list[dict], tolerance: float = 3) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        if not lines or abs(word["top"] - lines[-1][0]["top"]) > tolerance:
            lines.append([word])
        else:
            lines[-1].append(word)
    return [sorted(line, key=lambda item: item["x0"]) for line in lines]


def _house_text_page(current, following=None) -> list[dict[str, Any]]:
    words = current.extract_words()
    lines = _lines(words)
    headers = next((line for line in lines if {"Owner", "Asset", "Amount"}.issubset(
        {word["text"] for word in line})), None)
    if not headers:
        return []
    positions = {word["text"]: word["x0"] for word in headers}
    if not {"Owner", "Asset", "Transaction", "Date", "Notification", "Amount"}.issubset(positions):
        return []
    fields = ["owner", "asset", "direction", "date", "notification", "amount"]
    starts = [positions[name] - 2 for name in
              ["Owner", "Asset", "Transaction", "Date", "Notification", "Amount"]]
    ends = starts[1:] + [positions.get("Cap.", current.width)]
    anchors = [line[0]["top"] for line in lines if any(
        starts[3] <= word["x0"] < ends[3] and re.fullmatch(_DATE, word["text"])
        for word in line) and any(
            starts[2] <= word["x0"] < ends[2] and _direction(word["text"]) != "unknown"
            for word in line)]
    rows = []
    for number, top in enumerate(anchors, 1):
        bottom = anchors[number] if number < len(anchors) else current.height
        selected = [line for line in lines if top - 2 <= line[0]["top"] < bottom - 2]
        raw_lines = [_clean(" ".join(word["text"] for word in line)) for line in selected]
        cells: dict[str, list[str]] = {field: [] for field in fields}
        description_lines = []
        commentary = False
        for line, raw_line in zip(selected, raw_lines, strict=True):
            if re.match(r"(?:F\s*S\s*:|Filing Status|D\s*:|Description)", raw_line):
                commentary = True
            if commentary:
                description_lines.append(raw_line)
                continue
            for field, start, end in zip(fields, starts, ends, strict=True):
                cells[field].extend(word["text"] for word in line if start <= word["x0"] < end)
        if number == len(anchors) and following is not None:
            next_text = following.extract_text() or ""
            leading = []
            for line in next_text.splitlines():
                if re.search(_DATE, line) and _DIRECTION.search(line):
                    break
                if re.match(r"D[\x00\s]*:", line) or leading:
                    leading.append(line)
            description_lines.extend(leading)
            for next_line in _lines(following.extract_words()):
                if any(starts[3] <= word["x0"] < ends[3] and re.fullmatch(_DATE, word["text"])
                       for word in next_line) and any(
                       starts[2] <= word["x0"] < ends[2] and _direction(word["text"]) != "unknown"
                       for word in next_line):
                    break
                next_raw = _clean(" ".join(word["text"] for word in next_line))
                if re.match(r"(?:D\s*:|Description|F\s*S\s*:)", next_raw):
                    break
                if not re.search(r"\[[A-Z]{1,3}\]|\$[\d,]+", next_raw):
                    continue
                # Asset and amount cells can continue before the next page's
                # first transaction. Header and description text cannot fill them.
                for field in ("asset", "amount"):
                    idx = fields.index(field)
                    cells[field].extend(word["text"] for word in next_line
                                        if starts[idx] <= word["x0"] < ends[idx])
                raw_lines.append(next_raw)
        values = {field: _clean(" ".join(parts)) for field, parts in cells.items()}
        values["description"] = " ".join(description_lines)
        # The electronic House owner column is blank for the filer themselves.
        values["owner"] = values["owner"] or "Self"
        # Footer material after the last row is never an asset description.
        values["asset"] = re.split(r"\* For the|I CERTIFY", values["asset"])[0].strip()
        if non_transaction_text(values["asset"], asset=True):
            continue
        rows.append(_row(values, "\n".join(raw_lines), ocr=False, number=number))
    return rows


def _grid_columns(picture, words: list[dict]) -> list[float]:
    """Locate printed table rules; recognition itself remains Tesseract's job."""
    import numpy as np

    dates = [word for word in words if re.fullmatch(_DATE, word["text"])]
    if len(dates) < 2:
        return []
    margin = 200 if len(dates) < 4 else 5
    top = max(0, int(min(word["top"] for word in dates)) - margin)
    bottom = min(picture.height, int(max(word["top"] for word in dates)) + max(20, margin))
    pixels = np.asarray(picture.convert("L"))[top:bottom]
    dark = pixels < 180
    candidates = []
    for column, vector in enumerate(dark.T):
        edges = np.diff(np.r_[False, vector, False].astype(np.int8))
        lengths = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
        if len(lengths) and max(lengths) >= 100:
            candidates.append(column)
    groups: list[list[int]] = []
    for column in candidates:
        if not groups or column > groups[-1][-1] + 3:
            groups.append([int(column)])
        else:
            groups[-1].append(int(column))
    return [sum(group) / len(group) for group in groups]


def _without_rules(picture, grid: list[float]):
    import numpy as np
    from PIL import Image

    pixels = np.asarray(picture.convert("L")).copy()
    dark = pixels < 180
    # Remove long printed rule segments, including slightly tilted scans. This
    # is raster preprocessing; every letter and checkbox is read by Tesseract.
    for axis in (0, 1):
        view = dark if axis == 0 else dark.T
        for index, vector in enumerate(view):
            edges = np.diff(np.r_[False, vector, False].astype(np.int8))
            for start, end in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
                if end - start < 60:
                    continue
                if axis == 0:
                    pixels[max(0, index - 1):index + 2, start:end] = 255
                else:
                    pixels[start:end, max(0, index - 1):index + 2] = 255
    return Image.fromarray(pixels)


def _grid_rows(picture, grid: list[float]) -> list[float]:
    import numpy as np

    if not grid:
        return []
    pixels = np.asarray(picture.convert("L"))
    dark = pixels[:, int(grid[0]):int(grid[-1])] < 180
    candidates = []
    for row, vector in enumerate(dark):
        edges = np.diff(np.r_[False, vector, False].astype(np.int8))
        lengths = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
        if len(lengths) and max(lengths) >= 120:
            candidates.append(row)
    groups: list[list[int]] = []
    for row in candidates:
        if not groups or row > groups[-1][-1] + 3:
            groups.append([int(row)])
        else:
            groups[-1].append(int(row))
    return [sum(group) / len(group) for group in groups]


def _refine_columns(picture, words: list[dict], grid: list[float], rules: list[float], source: str,
                    timeout: int) -> list[dict]:
    """Let Tesseract segment small numeric columns separately from asset text."""
    import pytesseract
    from PIL import Image, ImageOps

    intervals = list(zip(grid, grid[1:]))
    if len(intervals) < 5:
        return words
    asset_index = max(range(len(intervals)), key=lambda idx: intervals[idx][1] - intervals[idx][0])
    selected: list[tuple[float, float, str]] = []
    if source == "oge":
        for offset, alphabet in [(1, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ01()"), (2, "0123456789/-")]:
            if asset_index + offset < len(intervals):
                selected.append((*intervals[asset_index + offset], alphabet))
        if asset_index + 4 < len(intervals):
            selected.append((intervals[asset_index + 4][0], grid[-1], "0123456789$,-"))
    elif source == "house":
        frequencies = [(sum(bool(re.search(_DATE, word["text"]))
                            for word in words if start < word["x0"] < end), start, end)
                       for start, end in intervals]
        for count, start, end in sorted(frequencies, reverse=True)[:2]:
            if count >= 2:
                selected.append((start, end, "0123456789/-"))
    for start, end, alphabet in selected:
        left = int(start) + 1
        # Scanned numerals occasionally cross a printed cell rule. Keep a small
        # margin so the final digit is not clipped by the crop.
        right = min(picture.width, int(end) + 12) if alphabet == "0123456789/-" else int(end) - 1
        region = picture.crop((left, 0, right, picture.height))
        region = region.resize((region.width * 2, region.height * 2), Image.Resampling.LANCZOS)
        data = pytesseract.image_to_data(
            region, lang="eng", config="--psm 6 -c tessedit_char_whitelist=" + alphabet,
            output_type=pytesseract.Output.DICT, timeout=timeout,
        )
        refined = [{"text": data["text"][idx], "x0": left + data["left"][idx] / 2,
                    "top": data["top"][idx] / 2,
                    "bottom": (data["top"][idx] + data["height"][idx]) / 2,
                    "x1": left + (data["left"][idx] + data["width"][idx]) / 2}
                   for idx in range(len(data["text"])) if data["text"][idx].strip()]
        if refined:
            words = [word for word in words if not start < word["x0"] < end] + refined
        if source == "oge" and alphabet != "0123456789$,-":
            for top, bottom in zip(rules, rules[1:]):
                if not 12 <= bottom - top <= 250:
                    continue
                cell_words = [word for word in refined if top < word["top"] < bottom]
                crop_top = min((word["top"] for word in cell_words), default=top + 3) - 2
                crop_bottom = max((word["bottom"] for word in cell_words), default=bottom - 3) + 2
                cell = picture.crop((left, int(crop_top), right, int(crop_bottom)))
                cell = cell.resize((cell.width * 3, cell.height * 3), Image.Resampling.LANCZOS)
                original_cell = cell
                if alphabet != "0123456789/-":
                    # Faint scan streaks dominate short words such as Sale. A
                    # dark-ink mask and margin retain glyphs while excluding them.
                    cell = cell.convert("L").point(lambda pixel: 255 if pixel > 100 else 0)
                    cell = ImageOps.expand(cell, border=15, fill=255)
                value = pytesseract.image_to_string(
                    cell, lang="eng", config="--psm 7 -c tessedit_char_whitelist=" + alphabet,
                    timeout=timeout,
                ).strip()
                if alphabet != "0123456789/-" and _ocr_direction(value) == "unknown":
                    # The dark-ink pass can lose faint letters. Retry the same
                    # cell with its original grayscale, keeping the same bounded
                    # option matcher; an unreadable retry stays unknown.
                    retry = pytesseract.image_to_string(
                        ImageOps.expand(original_cell, border=15, fill=255), lang="eng",
                        config="--psm 7 -c tessedit_char_whitelist=" + alphabet,
                        timeout=timeout,
                    ).strip()
                    if _ocr_direction(retry) != "unknown":
                        value = retry
                # Direction and numeric columns use cell OCR as primary evidence. An
                # unreadable separator remains unknown; column-wide OCR must not
                # silently substitute a different, superficially valid date.
                words = [word for word in words if not (
                        start < word["x0"] < end and top < word["top"] < bottom)]
                words.append({"text": value, "x0": left, "x1": right,
                              "top": top + (bottom - top) / 3})
    return words


def _ocr_rows(words: list[dict], source: str, grid: list[float], rules: list[float],
              *, ocr: bool = True) -> list[dict[str, Any]]:
    lines = [[word for word in words if top < word["top"] < bottom]
             for top, bottom in zip(rules, rules[1:])] if rules else _lines(words, tolerance=8)
    if rules:
        # An incomplete bottom border must not discard the last transaction.
        tail = [word for word in words if word["top"] >= rules[-1]]
        if tail:
            lines.extend(_lines(tail, 8))
    # Some scans have missing horizontal rules. Split a merged band at distinct
    # date baselines so several transactions cannot become a single asset row.
    separated = []
    for line in lines:
        date_words = [word for word in line if re.search(r"\d{1,2}[/-]\d{1,2}[/-]", word["text"])]
        intervals = list(zip(grid, grid[1:]))
        if source == "oge" and len(intervals) >= 5:
            asset_index = max(range(len(intervals)), key=lambda idx: intervals[idx][1] - intervals[idx][0])
            if asset_index + 2 < len(intervals):
                start, end = intervals[asset_index + 2]
                date_words = [word for word in date_words if start < word["x0"] < end]
        date_lines = _lines(date_words, 8)
        if len(date_lines) <= 1:
            separated.append(line)
            continue
        anchors = [sum(word["top"] for word in group) / len(group) for group in date_lines]
        cuts = [float("-inf"), *[(left + right) / 2 for left, right in zip(anchors, anchors[1:])],
                float("inf")]
        separated.extend([[word for word in line if top <= word["top"] < bottom]
                          for top, bottom in zip(cuts, cuts[1:])])
    lines = separated
    rows = []
    header_bottom = max((word["top"] for word in words if word["text"].lower()
                         in {"description", "amount"}), default=0)
    for line in lines:
        if not line:
            continue
        dates = sorted([word for word in line if re.fullmatch(_DATE, word["text"])],
                       key=lambda item: item["x0"])
        intervals = list(zip(grid, grid[1:]))
        oge_asset = max(intervals, key=lambda span: span[1] - span[0]) if (
            source == "oge" and len(intervals) >= 5) else None
        numbered = bool(oge_asset and any(re.fullmatch(r"\d{1,5}", word["text"])
                                          for word in line if word["x0"] < oge_asset[0]))
        house_asset = max(intervals, key=lambda span: span[1] - span[0]) if (
            source == "house" and len(intervals) >= 8) else None
        house_candidate = bool(house_asset and len(line) >= 4 and any(
            house_asset[1] < word["x0"] and re.search(r"[/-]\d{2,4}\b", word["text"])
            for word in line))
        oge_candidate = bool(oge_asset and min(word["top"] for word in line) > header_bottom
                             and any("$" in word["text"] for word in line))
        if source == "oge" and min(word["top"] for word in line) <= header_bottom:
            continue
        if (not dates and not numbered and not oge_candidate and not house_candidate) or (
            source == "house" and len(dates) != 2 and not house_candidate
        ):
            continue
        raw = "\n".join(" ".join(word["text"] for word in row) for row in _lines(line, 8))
        if non_transaction_text(raw):
            continue
        # House paper forms use checkboxes; unreadable or unpositioned checks are
        # deliberately unknown rather than interpreted as a buy or a sell.
        direction_words = [word for word in line if _direction(word["text"]) != "unknown"]
        direction = direction_words[-1]["text"] if direction_words else ""
        asset_interval = None
        if source == "house":
            preceding = [(start, end) for start, end in intervals
                         if dates and end < dates[0]["x0"]]
            if preceding:
                asset_interval = max(preceding, key=lambda span: span[1] - span[0])
            elif house_asset:
                asset_interval = house_asset
        if oge_asset:
            asset_interval = oge_asset
        asset_end = asset_interval[1] if asset_interval else dates[0]["x0"] - 3
        asset_words = [word for word in line if word["x0"] < asset_end and
                       (not asset_interval or word["x0"] > asset_interval[0])]
        left = [word["text"] for fragment in _lines(asset_words, 8) for word in fragment]
        left = [value for value in left if value.lower() not in
                {"x", "purchase", "sale", "exchange", "p", "s", "e"}]
        asset = " ".join(left)
        asset = re.sub(r"^\d+[.)]?\s+", "", asset)
        if non_transaction_text(asset, asset=True):
            continue
        owner_match = re.match(r"^(SP|JT|DC|Self|Spouse|Joint)\b\s*", asset, re.I)
        owner = owner_match.group(1) if owner_match else ""
        if owner_match:
            asset = asset[owner_match.end():]
        if re.search(r"\bExample\b", asset, re.I):
            continue
        if not asset or len(asset) < 3:
            if source == "oge" and (numbered or oge_candidate):
                asset = ""
            else:
                continue
        if source == "house" and asset_interval:
            owner_words = [word["text"] for word in line if word["x0"] < asset_interval[0]]
            owner = next((word for word in owner_words if word.upper() in {"SP", "DC", "JT"}), "")
        values = {"asset": asset, "owner": owner, "direction": direction,
                  "date": dates[0]["text"] if dates else "", "notification": dates[1]["text"]
                  if source == "house" and len(dates) > 1 else "", "amount": raw}
        if oge_asset:
            asset_index = intervals.index(oge_asset)
            for name, offset in [("direction", 1), ("date", 2), ("amount", 4)]:
                if asset_index + offset < len(intervals):
                    start, end = intervals[asset_index + offset]
                    values[name] = " ".join(word["text"] for word in line
                                             if start < word["x0"] < end)
        if source == "house" and asset_interval:
            asset_index = intervals.index(asset_interval)
            first_date_index = next((idx for idx, (start, end) in enumerate(intervals)
                                     if dates and start < dates[0]["x0"] < end), None)
            four_directions = first_date_index == asset_index + 5
            direction_cells = intervals[asset_index + 1:asset_index + (5 if four_directions else 4)]
            marks = [word for word in line if word["text"].lower() in {"x", "*"}]
            selected = [idx for idx, (start, end) in enumerate(direction_cells)
                        if any(start < (word["x0"] + word["x1"]) / 2 < end for word in marks)]
            if len(selected) == 1:
                values["direction"] = (["Purchase", "Sale", "Partial Sale", "Exchange"] if four_directions
                                       else ["Purchase", "Sale", "Exchange"])[selected[0]]
            # Amount columns follow the notification date in the official paper
            # form; the last checkbox is the statutory spouse/child threshold.
            all_date_columns = [(sum(bool(re.search(_DATE, word["text"]))
                                    for word in words if start < word["x0"] < end), idx)
                                for idx, (start, end) in enumerate(intervals)]
            date_columns = sorted(idx for count, idx in sorted(all_date_columns, reverse=True)[:2]
                                  if count >= 1)
            if len(date_columns) == 2:
                for field, idx in zip(["date", "notification"], date_columns, strict=True):
                    start, end = intervals[idx]
                    values[field] = " ".join(word["text"] for word in line
                                             if start < word["x0"] < end)
            notification_index = date_columns[-1] if len(date_columns) == 2 else None
            if notification_index is not None:
                amount_cells = intervals[notification_index + 1:]
                selected = [idx for idx, (start, end) in enumerate(amount_cells)
                            if any(start < (word["x0"] + word["x1"]) / 2 < end
                                   for word in marks)]
                ranges = ["$1,001 - $15,000", "$15,001 - $50,000", "$50,001 - $100,000",
                          "$100,001 - $250,000", "$250,001 - $500,000",
                          "$500,001 - $1,000,000", "$1,000,001 - $5,000,000",
                          "$5,000,001 - $25,000,000", "$25,000,001 - $50,000,000",
                          "Over $50,000,000", "Over $1,000,000"]
                if len(amount_cells) == len(ranges) and len(selected) == 1:
                    values["amount"] = ranges[selected[0]]
        if source == "oge" and re.search(
            r"\b(?:REV|RF(DG|G)?|GO|MUD|BDS|SCH|CNTY)\b.*\bDue\b", asset, re.I
        ):
            # Published municipal debt descriptions identify the instrument by
            # bond-series abbreviations and maturity, without an equity symbol.
            values["asset_type"] = "municipal bond"
        normalized_direction = False
        if source == "oge" and not ocr:
            direction = _ocr_direction(values["direction"])
            normalized_direction = direction != "unknown" and direction != _direction(values["direction"])
            values["direction"] = direction
        row = _row(values, raw, ocr=ocr, number=len(rows) + 1)
        if normalized_direction:
            row["warnings"].append("direction_text_normalized")
            row["confidence"] = "review"
        rows.append(row)
    return rows


def _ocr_page(payload: bytes, page: int, source: str) -> tuple[str, list[dict], list[float], list[float]]:
    import pypdfium2
    import pytesseract

    from iirp.political.config import policy

    options = policy()["ocr"]
    os.environ["OMP_THREAD_LIMIT"] = str(options["threads"])
    document = pypdfium2.PdfDocument(payload)
    pdf_page = document[page - 1]
    bitmap = pdf_page.render(scale=options["dpi"] / 72)
    try:
        picture = bitmap.to_pil()
        rotation = 0
        try:
            orientation = pytesseract.image_to_osd(
                picture, output_type=pytesseract.Output.DICT,
                timeout=min(15, options["timeout_seconds"]),
            )
            rotation = orientation["rotate"]
        except pytesseract.TesseractError:
            pass

        def recognize(candidate):
            data = pytesseract.image_to_data(
                candidate, lang="eng", config="--psm 11",
                output_type=pytesseract.Output.DICT, timeout=options["timeout_seconds"],
            )
            return [{"text": data["text"][idx], "x0": data["left"][idx],
                     "top": data["top"][idx],
                     "x1": data["left"][idx] + data["width"][idx]}
                    for idx in range(len(data["text"])) if data["text"][idx].strip()]

        def score(candidate_words):
            return sum(3 * bool(_date(word["text"])) +
                       (_direction(word["text"]) != "unknown") +
                       (word["text"].lower() in {"description", "amount", "transactions"})
                       for word in candidate_words)

        # OSD can mistake a numeric transaction table for an upside-down page.
        # Retain the original orientation unless recognizable fields improve.
        words = recognize(picture)
        candidates = [-rotation] if rotation else []
        if source == "house" and score(words) < 6:
            candidates.extend([90, 270])
        original_picture = picture
        for turn in dict.fromkeys(candidates):
            candidate_picture = original_picture.rotate(turn, expand=True)
            candidate_words = recognize(candidate_picture)
            if score(candidate_words) > score(words):
                picture, words = candidate_picture, candidate_words
        grid = _grid_columns(picture, words)
        rules = _grid_rows(picture, grid)
        if source in {"house", "oge"}:
            clean_picture = _without_rules(picture, grid)
            data = pytesseract.image_to_data(
                clean_picture, lang="eng", config="--psm 6",
                output_type=pytesseract.Output.DICT, timeout=options["timeout_seconds"],
            )
            words = [{"text": data["text"][idx], "x0": data["left"][idx],
                      "top": data["top"][idx],
                      "x1": data["left"][idx] + data["width"][idx]}
                     for idx in range(len(data["text"])) if data["text"][idx].strip()]
            words = _refine_columns(clean_picture, words, grid, rules, source,
                                    options["timeout_seconds"])
        text = "\n".join(" ".join(word["text"] for word in line)
                         for line in _lines(words, tolerance=8))
        return text, words, grid, rules
    finally:
        bitmap.close()
        pdf_page.close()
        document.close()



def _metadata(text: str, source: str) -> dict[str, Any]:
    if source != "house":
        return {}
    # Only explicit electronic amendment statements identify a revision; paper
    # forms print the word Amendment next to an unchecked box on every cover.
    normalized = re.sub(r"\s+", " ", text.replace("\x00", ""))
    amended = bool(re.search(r"(?:Filing Status|F\s*S)\s*:\s*Amend(?:ed|ment)\b",
                             normalized, re.I))
    original = re.search(r"(?:amendment to|amends|original)\s+(?:filing|report)"
                         r"(?:\s+ID)?\s*#?\s*(\d{7,8})\b", normalized, re.I)
    if not amended and original is None:
        return {}
    result = {"amendment": True}
    if original:
        result["amendment_of_external_id"] = original[1]
    return result



def _paper_metadata(words: list[dict]) -> dict[str, Any]:
    # A printed Amendment label occurs even on original paper reports. Only an
    # explicitly recognized X in its own adjacent checkbox identifies a revision.
    labels = [word for word in words if word["text"].lower() == "amendment"]
    for label in labels:
        if not any(word["text"].lower().startswith("initial") and
                   word["x0"] < label["x0"] and abs(word["top"] - label["top"]) < 15
                   for word in words):
            continue
        if any(word["text"].strip("[]()").lower() == "x" and
               0 < label["x0"] - word["x1"] < 40 and
               abs(label["top"] - word["top"]) < 15 for word in words):
            return {"amendment": True}
    return {}


def parse_pdf_page(payload: bytes, page: int, source: str) -> dict[str, Any]:
    import pdfplumber

    started = time.monotonic()
    with pdfplumber.open(io.BytesIO(payload)) as document:
        if page < 1 or page > len(document.pages):
            raise ValueError("page_out_of_range")
        current = document.pages[page - 1]
        text = current.extract_text() or ""
        layer_words = current.extract_words() if source == "oge" else []
        damaged = len(re.findall(r"\(cid:\d+\)|\ufffd", text)) > max(3, len(text) // 100)
        usable_layer = len(re.sub(r"\s", "", text)) >= 40 and not damaged
        layer_text = text
        ocr = not usable_layer or source == "oge"
        rows = []
        if source == "house":
            following = document.pages[page] if page < len(document.pages) else None
            usable_rows = _house_text_page(current, following)
            if usable_rows and not any(re.search(r"\(cid:\d+\)|\ufffd", row["asset_name"])
                                       for row in usable_rows):
                rows, ocr = usable_rows, False
        if not ocr:
            if not rows:
                for table in current.extract_tables():
                    rows.extend(_table_rows(table))
    paper_metadata = {}
    if ocr:
        text, words, grid, rules = _ocr_page(payload, page, source)
        if source == "house":
            paper_metadata = _paper_metadata(words)
        rows = _ocr_rows(words, source, grid, rules)
    if not rows:
        rows = _text_rows(text, source=source, ocr=ocr)
    if source == "oge" and usable_layer and layer_words:
        checked = rows
        check_text, check_grid, check_rules = text, grid, rules
        from iirp.political.config import policy
        scale = policy()["ocr"]["dpi"] / 72
        positioned = [{**word, **{key: word[key] * scale for key in ("x0", "x1", "top")}}
                      for word in layer_words]
        primary = _ocr_rows(positioned, source, check_grid, check_rules, ocr=False)
        # OCR text layers in scans can contain readable headers but misplaced
        # transaction glyphs. Such a layer is unusable for a row-wise comparison.
        if not primary or len(primary) != len(checked):
            rows, text, ocr = checked, check_text, True
            for row in rows:
                row["warnings"] = [*row["warnings"], "text_layer_unusable"]
                row["confidence"] = "review"
        else:
            rows, text, ocr = primary, layer_text, False
            for index, row in enumerate(rows):
                if row["transaction_type"] != checked[index]["transaction_type"]:
                    row["transaction_type"] = "unknown"
                    row["confidence"] = "review"
                    row["warnings"] = [*row["warnings"], "direction_conflict"]
    rows = [row for row in rows if not non_transaction_text(row.get("asset_name"), asset=True)
            and not non_transaction_text(row.get("raw_row"))]
    for number, row in enumerate(rows, 1):
        row["row_number"] = number
    return {"raw_text": text, "ocr": ocr, "seconds": time.monotonic() - started,
            "rows": rows, **_metadata(text, source), **paper_metadata}


class _HTMLTables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(_clean("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_senate_html(payload: bytes) -> list[dict[str, Any]]:
    parser = _HTMLTables()
    parser.feed(payload.decode("utf-8", errors="replace"))
    rows = []
    for table in parser.tables:
        rows.extend(_table_rows(table))
    for number, row in enumerate(rows, 1):
        row["row_number"] = number
    return rows
