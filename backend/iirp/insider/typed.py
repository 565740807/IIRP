"""Queryable filing-time facts; no estimates are written into SEC transactions."""

import re
from decimal import Decimal, InvalidOperation


def decimal_value(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def truth(value):
    return str(value).lower() in {"1", "true"}


def role_flags(relationships):
    titles = [str(item.get("officer_title") or "") for item in relationships
              if truth(item.get("is_officer")) or truth(item.get("is_director"))]
    title = " | ".join(" ".join(value.split()) for value in titles)
    return {
        "is_ceo": bool(re.search(r"\b(?:CEO|Chief\s+Executive\s+Officer)\b", title, re.I)),
        "is_cfo": bool(re.search(r"\b(?:CFO|Chief\s+Financial\s+Officer)\b", title, re.I)),
        # A Vice President is not the President.
        "is_president": bool(re.search(r"\bPresident\b", re.sub(r"\bVice[ -]+President\b", "", title, flags=re.I), re.I)),
        "is_chair": bool(re.search(r"\b(?:Chairman|Chairwoman|Chairperson|Chair)\b", title, re.I)),
        "is_director": any(truth(item.get("is_director")) for item in relationships),
        "is_ten_percent": any(truth(item.get("is_ten_percent_owner")) for item in relationships),
    }


_NUMBER = r"\$?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
_RANGE = re.compile(r"(?:ranging\s+from|range(?:d|s)?\s+(?:from|of)|prices\s+(?:from|between)|between)\s*"
                    + _NUMBER + r"\s*(?:to|and|[-–])\s*" + _NUMBER, re.I)


def weighted_price_range(footnotes):
    """Only explicit weighted-average footnotes and an unambiguous price range."""
    found = set()
    for note in footnotes or []:
        text = note.get("text", "") if isinstance(note, dict) else str(note)
        if not re.search(r"weighted[\s-]+average", text, re.I):
            continue
        for match in _RANGE.finditer(text):
            low, high = (decimal_value(value.replace(",", "")) for value in match.groups())
            if low is not None and high is not None and 0 <= low <= high:
                found.add((low, high))
    return next(iter(found)) if len(found) == 1 else (None, None)


def typed_fields(data, relationships=None):
    shares, price = decimal_value(data.get("shares")), decimal_value(data.get("price_per_share"))
    low, high = weighted_price_range(data.get("footnotes"))
    notes = " ".join(str(note.get("text", "")) for note in data.get("footnotes", []) if isinstance(note, dict))
    notes = " ".join(notes.split())
    # A negative plan statement is not affirmative plan evidence.
    clauses = re.split(r"[.;]", notes)
    plan_note = any(re.search(r"(?:pursuant\s+to|under|in\s+accordance\s+with).*?10b5[\s-]*1", clause, re.I)
                    and not re.search(r"\b(?:not|no|without)\b.*?10b5[\s-]*1", clause, re.I) for clause in clauses)
    return {
        "transaction_code": data.get("code"), "trade_direction": data.get("direction"),
        "trade_shares": shares, "reported_price": price,
        "reported_amount": shares * price if shares is not None and price is not None else None,
        "ownership_type": data.get("direct_or_indirect"),
        "is_plan": truth(data.get("raw_10b5_1_flag")) or plan_note,
        **role_flags(relationships if relationships is not None else list((data.get("owner_relationships") or {}).values())),
        "price_range_low": low, "price_range_high": high,
    }
