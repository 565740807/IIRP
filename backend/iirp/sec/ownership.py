"""Bounded SEC ownership XML (Form 3/4/5) reader.

Preserves all owners and source rows. A row is not replicated per owner, and no
claim of economic uniqueness is made across filings. Amendments stay unresolved.
Raw bytes are retained for evidence; fixture provenance must be explicit.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from re import fullmatch
from typing import Literal
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException


class OwnershipParseError(ValueError):
    """Input is unsafe, exceeds a bound, or is not a supported ownership filing."""


@dataclass(frozen=True)
class ParseLimits:
    max_bytes: int = 1_048_576
    max_nodes: int = 20_000
    max_depth: int = 64
    max_rows: int = 2_000


@dataclass(frozen=True)
class OwnerObservation:
    cik: str | None
    raw_cik: str | None
    name: str | None
    is_director: str | None
    is_officer: str | None
    is_ten_percent_owner: str | None
    is_other: str | None
    officer_title: str | None
    other_text: str | None
    raw_xml: bytes


@dataclass(frozen=True)
class Footnote:
    id: str | None
    text: str


@dataclass(frozen=True)
class OwnershipRow:
    table: Literal["I", "II"]
    source_row_index: int
    row_kind: Literal["holding", "transaction"]
    is_trade_observation: bool
    action_category: str
    security_title: str | None
    transaction_date: date | None
    deemed_execution_date: date | None
    code: str | None
    direction: str | None
    transaction_form_type: str | None
    shares: Decimal | None
    quantity_unit: str | None
    price_per_share: Decimal | None
    currency: str | None
    exercise_price: Decimal | None
    exercise_date: date | None
    expiration_date: date | None
    underlying_security_title: str | None
    underlying_shares: Decimal | None
    direct_or_indirect: str | None
    nature_of_ownership: str | None
    shares_after: Decimal | None
    footnote_ids: tuple[str, ...]
    raw_xml: bytes
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class FilingObservation:
    accession: str | None
    source_kind: Literal["synthetic", "sec", "manual"]
    source_url: str | None
    content_sha256: str
    raw_xml: bytes
    form_type: str
    raw_document_type: str
    is_amendment: bool
    amendment_resolution: Literal["unresolved", "not_applicable"]
    period_of_report: date | None
    issuer_cik: str | None
    raw_issuer_cik: str | None
    issuer_name: str | None
    issuer_ticker: str | None
    owners: tuple[OwnerObservation, ...]
    rows: tuple[OwnershipRow, ...]
    footnotes: tuple[Footnote, ...]
    raw_10b5_1_flag: str | None
    warnings: tuple[str, ...]


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child(element: ET.Element | None, tag: str) -> ET.Element | None:
    return (
        next((child for child in element if _tag(child) == tag), None)
        if element is not None
        else None
    )


def _text(element: ET.Element | None, path: str) -> str | None:
    for part in path.split("/"):
        element = _child(element, part)
    if element is None:
        return None
    value = _child(element, "value")
    # Never obtain a structured number by reading a linked footnote's text.
    text = (value.text if value is not None else element.text) or ""
    return text.strip() or None


def _cik(raw: str | None) -> str | None:
    return raw.zfill(10) if raw and raw.isascii() and raw.isdigit() and 0 < len(raw) <= 10 else None


def _date(raw: str | None, field: str, warnings: list[str]) -> date | None:
    if raw is None:
        return None
    try:
        value = date.fromisoformat(raw)
        if value.isoformat() != raw:
            raise ValueError("Date is not YYYY-MM-DD")
        return value
    except ValueError:
        warnings.append(f"invalid_date:{field}")
        return None


def _number(raw: str | None, field: str, warnings: list[str]) -> Decimal | None:
    if raw is None:
        return None
    try:
        if fullmatch(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", raw) is None:
            raise InvalidOperation
        value = Decimal(raw)
        if not value.is_finite() or value < 0:
            raise InvalidOperation
        return value
    except InvalidOperation:
        warnings.append(f"invalid_number:{field}")
        return None


def _action(kind: str, form: str, code: str | None, direction: str | None) -> str:
    if kind == "holding":
        return "holding"
    if form in {"3", "3/A"} or code is None or direction not in {"A", "D"}:
        return "needs_review"
    if (code == "P" and direction != "A") or (code == "S" and direction != "D"):
        return "needs_review"
    return {
        "P": "purchase_market_or_private",
        "S": "sale_market_or_private",
        "A": "grant_or_award",
        "F": "tax_or_exercise_price_withholding",
        "M": "exercise_or_conversion",
        "X": "exercise_or_conversion",
        "C": "exercise_or_conversion",
    }.get(code, "other")


def _row(
    element: ET.Element, table: str, index: int, form: str, footnotes: set[str]
) -> OwnershipRow:
    warnings = []
    kind = "holding" if _tag(element).endswith("Holding") else "transaction"
    code = _text(element, "transactionCoding/transactionCode")
    direction = _text(element, "transactionAmounts/transactionAcquiredDisposedCode")
    references = tuple(
        dict.fromkeys(
            child.get("id")
            for child in element.iter()
            if _tag(child) == "footnoteId" and child.get("id")
        )
    )
    for reference in references:
        if reference not in footnotes:
            warnings.append(f"missing_footnote:{reference}")
    if kind == "transaction" and form in {"3", "3/A"}:
        warnings.append("form3_transaction_requires_review")
    if kind == "transaction" and (code is None or direction not in {"A", "D"}):
        warnings.append("incomplete_transaction_coding")
    if (code == "P" and direction != "A") or (code == "S" and direction != "D"):
        warnings.append("code_direction_conflict")

    def number(path: str, label: str) -> Decimal | None:
        return _number(_text(element, path), label, warnings)

    def date_value(path: str, label: str) -> date | None:
        return _date(_text(element, path), label, warnings)

    # Evaluation of parsed fields appends warnings before their final snapshot.
    values = dict(
        table=table,
        source_row_index=index,
        row_kind=kind,
        is_trade_observation=kind == "transaction" and form not in {"3", "3/A"},
        action_category=_action(kind, form, code, direction),
        security_title=_text(element, "securityTitle"),
        transaction_date=date_value("transactionDate", "transaction_date"),
        deemed_execution_date=date_value("deemedExecutionDate", "deemed_execution_date"),
        code=code,
        direction=direction,
        transaction_form_type=_text(element, "transactionCoding/transactionFormType"),
        shares=number("transactionAmounts/transactionShares", "shares"),
        quantity_unit=None,
        price_per_share=number("transactionAmounts/transactionPricePerShare", "price_per_share"),
        currency=None,
        exercise_price=number("conversionOrExercisePrice", "exercise_price"),
        exercise_date=date_value("exerciseDate", "exercise_date"),
        expiration_date=date_value("expirationDate", "expiration_date"),
        underlying_security_title=_text(element, "underlyingSecurity/underlyingSecurityTitle"),
        underlying_shares=number(
            "underlyingSecurity/underlyingSecurityShares", "underlying_shares"
        ),
        direct_or_indirect=_text(element, "ownershipNature/directOrIndirectOwnership"),
        nature_of_ownership=_text(element, "ownershipNature/natureOfOwnership"),
        shares_after=number(
            "postTransactionAmounts/sharesOwnedFollowingTransaction", "shares_after"
        ),
        footnote_ids=references,
        raw_xml=ET.tostring(element, encoding="utf-8"),
    )
    return OwnershipRow(**values, warnings=tuple(warnings))


def parse_ownership_xml(
    content: bytes | str,
    *,
    source_kind: Literal["synthetic", "sec", "manual"],
    accession: str | None = None,
    source_url: str | None = None,
    limits: ParseLimits = ParseLimits(),
) -> FilingObservation:
    """Parse one document, retaining exact bytes; source_kind is caller-provided provenance."""
    if source_kind not in {"synthetic", "sec", "manual"}:
        raise ValueError("source_kind must explicitly identify synthetic, sec, or manual input")
    if any(type(bound) is not int or bound < 1 for bound in vars(limits).values()):
        raise ValueError("Parse limits must be positive integers")
    if not isinstance(content, (bytes, str)):
        raise TypeError("XML must be bytes or str")
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > limits.max_bytes:
        raise OwnershipParseError("XML byte limit exceeded")
    try:
        root = SafeET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ET.ParseError, ValueError) as exc:
        raise OwnershipParseError("Unsafe or malformed XML") from exc
    if _tag(root) != "ownershipDocument":
        raise OwnershipParseError("Expected ownershipDocument root")
    stack, count = [(root, 1)], 0
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > limits.max_nodes or depth > limits.max_depth:
            raise OwnershipParseError("XML node/depth limit exceeded")
        stack.extend((child, depth + 1) for child in element)
    original_form = _text(root, "documentType") or ""
    form = original_form.upper().replace(" ", "")
    if form in {"3A", "4A", "5A"}:
        form = form[0] + "/A"
    if form not in {"3", "3/A", "4", "4/A", "5", "5/A"}:
        raise OwnershipParseError("Unsupported Ownership documentType")
    warnings = []
    footnote_parent = _child(root, "footnotes")
    footnotes = tuple(
        Footnote(child.get("id"), "".join(child.itertext()).strip())
        for child in (() if footnote_parent is None else footnote_parent)
        if _tag(child) == "footnote"
    )
    ids = {note.id for note in footnotes if note.id}
    if len(ids) != len(footnotes):
        warnings.append("duplicate_or_missing_footnote_id")
    owners = []
    for child in root:
        if _tag(child) != "reportingOwner":
            continue
        raw_cik = _text(child, "reportingOwnerId/rptOwnerCik")
        if _cik(raw_cik) is None:
            warnings.append("missing_or_invalid_owner_cik")
        owners.append(
            OwnerObservation(
                cik=_cik(raw_cik),
                raw_cik=raw_cik,
                name=_text(child, "reportingOwnerId/rptOwnerName"),
                is_director=_text(child, "reportingOwnerRelationship/isDirector"),
                is_officer=_text(child, "reportingOwnerRelationship/isOfficer"),
                is_ten_percent_owner=_text(child, "reportingOwnerRelationship/isTenPercentOwner"),
                is_other=_text(child, "reportingOwnerRelationship/isOther"),
                officer_title=_text(child, "reportingOwnerRelationship/officerTitle"),
                other_text=_text(child, "reportingOwnerRelationship/otherText"),
                raw_xml=ET.tostring(child, encoding="utf-8"),
            )
        )
    if not owners:
        warnings.append("no_reporting_owners")
    rows = []
    for table_element in root:
        table_name = _tag(table_element)
        if table_name not in {"nonDerivativeTable", "derivativeTable"}:
            continue
        table = "I" if table_name == "nonDerivativeTable" else "II"
        expected_prefix = "nonDerivative" if table == "I" else "derivative"
        for index, element in enumerate(table_element, 1):
            if _tag(element) not in {expected_prefix + "Holding", expected_prefix + "Transaction"}:
                warnings.append(f"unrecognized_table_child:{table}:{index}")
                continue
            if len(rows) >= limits.max_rows:
                raise OwnershipParseError("Ownership row limit exceeded")
            rows.append(_row(element, table, index, form, ids))
    raw_issuer_cik = _text(root, "issuer/issuerCik")
    if _cik(raw_issuer_cik) is None:
        warnings.append("missing_or_invalid_issuer_cik")
    period = _date(_text(root, "periodOfReport"), "period_of_report", warnings)
    return FilingObservation(
        accession=accession,
        source_kind=source_kind,
        source_url=source_url,
        content_sha256=sha256(raw).hexdigest(),
        raw_xml=raw,
        form_type=form,
        raw_document_type=original_form,
        is_amendment=form.endswith("/A"),
        amendment_resolution="unresolved" if form.endswith("/A") else "not_applicable",
        period_of_report=period,
        issuer_cik=_cik(raw_issuer_cik),
        raw_issuer_cik=raw_issuer_cik,
        issuer_name=_text(root, "issuer/issuerName"),
        issuer_ticker=_text(root, "issuer/issuerTradingSymbol"),
        owners=tuple(owners),
        rows=tuple(rows),
        footnotes=footnotes,
        raw_10b5_1_flag=_text(root, "aff10b5One"),
        warnings=tuple(warnings),
    )
