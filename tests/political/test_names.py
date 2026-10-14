"""Name inference is conservative and deterministic without provider requests."""
from iirp.political.names import NameMatcher, normalize_name
from iirp.political.parse import _table_rows


def test_normalization_preserves_numbers_and_repairs_only_mixed_words():
    assert normalize_name("MCD0NALD5 CORP. Common Stock Class A USD0.50") == "MCDONALDS"
    assert normalize_name("META PLATFORMS INC.") == "META PLATFORMS"
    assert normalize_name("ACME 50 Company Ltd PLC") == "ACME 50"
    assert normalize_name("Microsoft Corporation COM") == "MICROSOFT"


def test_unique_exact_fuzzy_and_ambiguous_matches():
    matcher = NameMatcher([("Meta Platforms Inc.", "META"), ("Microsoft Corp", "MSFT"),
                           ("Alphabet Inc. Class A", "GOOGL"), ("Alphabet Inc. Class B", "GOOG")])
    assert matcher.match("META PLATFORMS") == ("META", "local_exact")
    assert matcher.match("Microsott Corporation") == (None, None)
    assert matcher.match("Meta Platform Inc") == ("META", "inferred")
    assert matcher.match("Alphabet Inc.") == (None, None)
    assert matcher.match("Alphabet Incorporatec") == (None, None)
    assert NameMatcher([("Meta Platform", "OTHER"), ("Meta Platforms", "META")], threshold=0.9).match(
        "Meta Platformz") == (None, None)


def test_bonds_and_funds_are_never_equity_transactions():
    matcher = NameMatcher([("Netflix Inc", "NFLX"), ("Acme Fund", "FUND")])
    row = _table_rows([["Asset", "Transaction Type", "Date", "Amount"],
                      ["NETFLIX INC 4.875% 2028", "Purchase", "01/01/2025", "$1,001 - $15,000"]])[0]
    matcher.apply(row)
    assert row["asset_type"] == "bond" and row["ticker"] is None
    assert row["issuer_ticker"] == "NFLX" and row["issuer_ticker_origin"] == "local_exact"
    fund = {"asset_name": "Acme Fund", "asset_type": "fund", "ticker": None}
    matcher.apply(fund)
    assert fund["ticker"] is None


def test_unknown_equity_can_match_without_changing_original_name():
    matcher = NameMatcher([("Meta Platforms Inc", "META")])
    row = {"asset_name": "META PLATFORMS INC", "asset_type": "unknown", "ticker": None,
           "warnings": ["ocr", "unknown_asset_type"], "confidence": "review"}
    matcher.apply(row)
    assert row["asset_name"] == "META PLATFORMS INC"
    assert row["ticker"] == "META" and row["asset_type"] == "stock"
    assert row["warnings"] == ["ocr"] and row["confidence"] == "review"
