"""Offline parser checks with synthetic disclosures; no source requests."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from iirp.political.parse import (
    _metadata,
    _money,
    _paper_metadata,
    _table_rows,
    _text_rows,
    inspect_pdf,
    parse_pdf_page,
    parse_senate_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_electronic_pdf_preserves_spouse_options_and_disclosure_ranges():
    payload = (FIXTURES / "electronic.pdf").read_bytes()
    assert inspect_pdf(payload) == 2
    parsed = parse_pdf_page(payload, 1, "house")
    assert not parsed["ocr"]
    assert parsed["seconds"] >= 0
    assert len(parsed["rows"]) == 6
    first, second, third = parsed["rows"][:3]
    assert first["owner"] == "spouse"
    assert first["ticker"] == "GOOGL"
    assert first["transaction_date"] == date(2025, 1, 14)
    assert first["notification_date"] == date(2025, 1, 14)
    assert first["amount_low"] == Decimal(250001)
    assert first["amount_high"] == Decimal(500000)
    assert first["transaction_type"] == "purchase"
    assert first["confidence"] == "parsed"
    assert second["asset_type"] == "option"
    assert second["option_type"] == "call"
    assert second["strike_price"] == Decimal("150")
    assert second["expiration_date"] == date(2026, 1, 16)
    assert third["transaction_type"] == "partial_sale"
    assert third["ticker"] == "AAPL"
    assert parsed["rows"][-1]["strike_price"] == Decimal(80)
    assert parsed["rows"][-1]["expiration_date"] == date(2026, 1, 16)


def test_scanned_pdf_uses_real_ocr_and_marks_every_row_for_review():
    payload = (FIXTURES / "scanned.pdf").read_bytes()
    parsed = parse_pdf_page(payload, 1, "house")
    assert parsed["ocr"]
    assert len(parsed["rows"]) == 17
    row = parsed["rows"][0]
    assert row["ticker"] is None
    assert row["transaction_date"] == date(2025, 1, 14)
    assert row["transaction_type"] == "purchase"
    assert row["amount_low"] == Decimal(50001)
    assert row["amount_high"] == Decimal(100000)
    assert row["confidence"] == "review"
    assert "ocr" in row["warnings"]
    assert all(value["confidence"] == "review" for value in parsed["rows"])


def test_oge_scanned_bond_page_uses_real_ocr():
    parsed = parse_pdf_page((FIXTURES / "oge.pdf").read_bytes(), 1, "oge")
    assert parsed["ocr"]
    assert len(parsed["rows"]) == 33
    first = parsed["rows"][0]
    # Tesseract reads the italic separators as digits in this damaged layer.
    # Preserve the unknown date instead of repairing ambiguous OCR characters.
    assert first["transaction_date"] in (None, date(2026, 7, 7))
    if first["transaction_date"] is None:
        assert "missing_transaction_date" in first["warnings"]
    assert first["transaction_type"] in ("unknown", "purchase")
    if first["transaction_type"] == "unknown":
        assert "unknown_transaction_type" in first["warnings"]
    assert first["asset_type"] == "bond"
    assert first["ticker"] is None
    assert first["amount_low"] == Decimal(1000001)
    assert first["amount_high"] == Decimal(5000000)
    assert first["confidence"] == "review"


def test_senate_html_uses_column_headers_and_keeps_unknowns():
    rows = parse_senate_html((FIXTURES / "senate.html").read_bytes())
    assert len(rows) == 2
    assert rows[0]["owner"] == "joint"
    assert rows[0]["ticker"] == "MSFT"
    assert rows[0]["transaction_date"] == date(2025, 5, 1)
    assert rows[0]["transaction_type"] == "sale"
    assert rows[0]["amount_low"] == Decimal(15001)
    assert rows[0]["amount_high"] == Decimal(50000)
    assert "Microsoft" in rows[0]["raw_row"]
    assert rows[1]["transaction_date"] is None
    assert rows[1]["ticker"] is None
    assert rows[1]["confidence"] == "review"


def test_oge_bond_does_not_map_identifiers_to_tickers():
    rows = _table_rows([
        ["Asset Description", "Owner", "Transaction Type", "Date", "Amount"],
        ["New York Municipal Bond (NYC) 5% due 2030", "Self", "Purchase",
         "08/15/2025", "$250,001 - $500,000"],
    ])
    row = rows[0]
    assert row["asset_type"] == "bond"
    assert row["ticker"] is None
    assert row["ticker_origin"] is None
    assert row["transaction_date"] == date(2025, 8, 15)
    assert row["confidence"] == "parsed"


def test_partial_or_invalid_rows_survive_as_review_without_invented_values():
    rows = _text_rows(
        "1 JT Unknown Holding Purchase 02/30/2025 $1,001 - $15,000\n"
        "2 SP Apple (AAPL) [ST] S 03/01/2025 03/05/2025 $50,001 - $100,000",
        source="house", ocr=False,
    )
    assert len(rows) == 2
    assert rows[0]["transaction_date"] is None
    assert rows[0]["ticker"] is None
    assert rows[0]["confidence"] == "review"
    assert rows[1]["owner"] == "spouse"
    assert rows[1]["transaction_type"] == "sale"


def test_amount_range_does_not_accept_reversed_or_corrupt_ranges():
    assert _money("$50,000 - $1,001") == (None, None)
    assert _money("$1,00l - $15,000") == (None, None)
    assert _money("Over $50,000,000") == (Decimal(50000001), None)
    assert _money("$0 - $1,000") == (Decimal(0), Decimal(1000))
    assert _money("$15.00") == (Decimal("15.00"), Decimal("15.00"))


def test_repeated_headers_do_not_become_transaction_rows():
    rows = _table_rows([
        ["Owner", "Asset", "Transaction Type", "Date", "Amount"],
        ["SP", "Apple Inc. (AAPL) [ST]", "P", "01/01/2025", "$1,001 - $15,000"],
        ["Owner", "Asset", "Transaction Type", "Date", "Amount"],
    ])
    assert len(rows) == 1


def test_option_missing_contract_fields_remains_reviewable():
    row = _table_rows([
        ["Owner", "Asset", "Transaction Type", "Date", "Amount"],
        ["SP", "Apple Inc. (AAPL) call options [OP]", "P", "01/01/2025", "$1,001 - $15,000"],
    ])[0]
    assert row["option_type"] == "call"
    assert row["strike_price"] is None
    assert row["expiration_date"] is None
    assert "incomplete_option" in row["warnings"]


def test_explicit_amendment_metadata_requires_an_original_identifier_for_linking():
    assert _metadata("Filing Status: Amendment; Amendment to filing ID #20026590", "house") == {
        "amendment": True, "amendment_of_external_id": "20026590"}
    assert _metadata("Filing Status: Amended", "house") == {"amendment": True}
    assert _metadata("Initial Report [X] Amendment [ ] Date of Report Being Amended:", "house") == {}
    assert _metadata("Filing Status: New", "house") == {}


def test_oge_equity_table_keeps_original_orientation_and_unreadable_asset_row():
    parsed = parse_pdf_page((FIXTURES / "oge-equity.pdf").read_bytes(), 1, "oge")
    assert parsed["ocr"] and len(parsed["rows"]) == 33
    first = parsed["rows"][0]
    assert first["asset_name"] == "Goldman Sachs Group Inc"
    assert first["transaction_date"] == date(2026, 6, 23)
    assert first["transaction_type"] == "purchase"
    assert first["amount_low"] == Decimal(1001)
    assert first["ticker"] is None and first["confidence"] == "review"
    assert all(row["confidence"] == "review" for row in parsed["rows"])


def test_description_expiration_date_is_not_a_second_transaction():
    parsed = parse_pdf_page((FIXTURES / "electronic.pdf").read_bytes(), 2, "house")
    assert len(parsed["rows"]) == 3
    assert [row["ticker"] for row in parsed["rows"]] == ["PANW", "TEM", "VST"]


def test_asset_and_amount_cells_continue_across_electronic_pages():
    parsed = parse_pdf_page((FIXTURES / "electronic-spanning.pdf").read_bytes(), 1, "house")
    assert len(parsed["rows"]) == 9
    row = parsed["rows"][-1]
    assert row["ticker"] == "TEM"
    assert row["amount_low"] == Decimal(50001)
    assert row["amount_high"] == Decimal(100000)
    assert row["transaction_date"] == date(2026, 1, 16)


def test_reported_symbols_are_not_discarded_as_form_type_codes():
    for ticker in ["CB", "OP", "ST", "SP"]:
        row = _table_rows([
            ["Owner", "Asset", "Transaction Type", "Date", "Amount"],
            ["SP", f"Reported Equity ({ticker}) [ST]", "P", "01/01/2025", "$1,001 - $15,000"],
        ])[0]
        assert row["ticker"] == ticker and row["asset_type"] == "stock"


def test_bond_fund_keeps_its_reported_fund_symbol():
    row = _table_rows([
        ["Owner", "Asset", "Transaction Type", "Date", "Amount"],
        ["Self", "Reported Bond ETF (BOND) [EF]", "P", "01/01/2025", "$1,001 - $15,000"],
    ])[0]
    assert row["asset_type"] == "fund" and row["ticker"] == "BOND"


def test_paper_amendment_requires_the_mark_in_its_own_checkbox():
    words = [{"text": "Initial", "x0": 320, "x1": 380, "top": 10},
             {"text": "Amendment", "x0": 620, "x1": 700, "top": 10}]
    initial = {"text": "[X]", "x0": 290, "x1": 305, "top": 12}
    amended = {"text": "X", "x0": 590, "x1": 605, "top": 12}
    assert _paper_metadata([*words, initial]) == {}
    assert _paper_metadata([*words, amended]) == {"amendment": True}
    assert _paper_metadata(words) == {}


def test_ocr_directions_require_a_unique_nearby_fixed_option():
    from iirp.political.parse import _ocr_direction

    assert _ocr_direction("purchaso") == "purchase"
    assert _ocr_direction("Sa1e") == "sale"
    assert _ocr_direction("Partial Sa1e") == "partial_sale"
    assert _ocr_direction("Exchango") == "exchange"
    assert _ocr_direction("purchase sale") == "unknown"
    assert _ocr_direction("ale exchange") == "unknown"
    assert _ocr_direction("xyz") == "unknown"
    # Removing characters must not turn a combined/ambiguous value into an option.
    assert _ocr_direction("P/S") == "unknown"


def test_oge_usable_text_is_primary_and_disagreeing_ocr_requires_review(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    import pdfplumber
    from iirp.political import parse

    table = [["Asset", "Owner", "Transaction Type", "Date", "Amount"],
             ["Text Layer Equity", "Self", "Purchase", "01/01/2025", "$1,001 - $15,000"]]
    page = SimpleNamespace(extract_text=lambda: "A usable original text layer for the transaction table",
                           extract_tables=lambda: [table], extract_words=lambda: [{"text": "layer", "x0": 0, "x1": 1, "top": 0}], images=[])
    monkeypatch.setattr(pdfplumber, "open", lambda _: nullcontext(SimpleNamespace(pages=[page])))
    monkeypatch.setattr(parse, "_ocr_page", lambda *_: ("OCR evidence", [], [], []))
    def evidence(*args, **kwargs):
        if kwargs.get("ocr") is False:
            return _table_rows(table)
        return [{"transaction_type": "purchase"}]
    monkeypatch.setattr(parse, "_ocr_rows", evidence)
    agreed = parse_pdf_page(b"synthetic", 1, "oge")
    assert agreed["rows"][0]["asset_name"] == "Text Layer Equity" and not agreed["ocr"]
    assert agreed["rows"][0]["transaction_type"] == "purchase"
    def conflict(*args, **kwargs):
        if kwargs.get("ocr") is False:
            return _table_rows(table)
        return [{"transaction_type": "sale"}]
    monkeypatch.setattr(parse, "_ocr_rows", conflict)
    row = parse_pdf_page(b"synthetic", 1, "oge")["rows"][0]
    assert row["transaction_type"] == "unknown" and row["confidence"] == "review"
    assert "direction_conflict" in row["warnings"]
    assert row["asset_name"] == "Text Layer Equity"


def test_oge_maturity_date_inside_asset_does_not_split_the_transaction():
    from iirp.political.parse import _ocr_rows

    words = [
        {"text": "1", "x0": 5, "x1": 10, "top": 40},
        {"text": "Bond", "x0": 22, "x1": 50, "top": 40},
        {"text": "02/15/2034", "x0": 55, "x1": 85, "top": 55},
        {"text": "Purchase", "x0": 105, "x1": 115, "top": 40},
        {"text": "10/15/2025", "x0": 125, "x1": 138, "top": 40},
        {"text": "$1,001-$15,000", "x0": 165, "x1": 199, "top": 40},
    ]
    rows = _ocr_rows(words, "oge", [0, 20, 100, 120, 140, 160, 200], [30, 65])
    assert len(rows) == 1 and rows[0]["transaction_type"] == "purchase"
    assert rows[0]["transaction_date"] == date(2025, 10, 15)


@pytest.mark.parametrize("text", [
    "OGE Received: 9/8/2026", "OGE RECEIVED", "OGE Form 278-T (2025)",
    "Page 2 of 9", "3 / 12", "12", "Transaction Type", "Amount of Transaction",
    "/s/ Filer, Name [electronically signed on 09/08/2026]",
    "Data Revised 09/08/2026", "Filer paid late fee 9/8/2026",
    "Transactions 13 On 4/2/2025 the filer received notification from the fund manager",
    "Note: This is a public form. Do not include account numbers or family member names.",
    "form. Do not include account numbers. See instructions for required information.",
    "8 Received Over 30 Type Days Ago",
])
def test_form_furniture_is_not_a_transaction(text):
    from iirp.political.parse import non_transaction_text

    assert non_transaction_text(text)


def test_receipt_stamp_and_signature_inside_oge_grid_are_excluded():
    from iirp.political.parse import _ocr_rows

    def band(number, asset, day, top):
        return [{"text": text, "x0": x, "x1": x + 9, "top": top} for x, text in
                [(5, str(number)), (25, asset), (105, "Purchase"), (125, day), (165, "$1,001-$15,000")]]
    words = band(1, "OGE Received", "09/08/2026", 40)
    words += band(2, "/s/ Person [electronically signed on 09/08/2026]", "09/08/2026", 80)
    words += band(3, "OGE Energy Corp", "09/01/2026", 120)
    rows = _ocr_rows(words, "oge", [0, 20, 100, 120, 140, 160, 200], [30, 65, 100, 140])
    assert len(rows) == 1 and rows[0]["asset_name"] == "OGE Energy Corp"
    assert rows[0]["transaction_type"] == "purchase"


def test_table_furniture_is_removed_but_damaged_trades_remain():
    rows = _table_rows([
        ["Asset", "Transaction Type", "Date", "Amount"],
        ["OGE Received", "", "09/08/2026", ""],
        ["Page 5 of 12", "", "09/08/2026", ""],
        ["OGE Energy Corp", "Unknown", "09/01/2026", ""],
        ["109", "Sale", "09/01/2026", "$1,001-$15,000"],
    ])
    assert [row["asset_name"] for row in rows] == ["OGE Energy Corp", "109"]
    assert rows[0]["transaction_type"] == "unknown" and rows[0]["amount_low"] is None
    assert rows[1]["amount_low"] == Decimal("1001")
