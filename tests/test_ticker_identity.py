"""SEC raw tokens remain readable, but placeholders cannot create market work."""

import pytest
from iirp.contracts import AnalysisInput, CollectionInput
from iirp.sec_facts import _compact_row, _ticker_read_view
from iirp.ticker_identity import normalized_ticker
from pydantic import ValidationError


def test_placeholder_is_unknown_with_raw_token_unchanged():
    for raw in ("none", " NONE ", "N/A", "unknown", "--"):
        assert normalized_ticker(raw) is None
    assert normalized_ticker(" aapl ") == "AAPL"


@pytest.mark.parametrize("model,extra", [
    (CollectionInput, {"kind": "market_history"}),
    (AnalysisInput, {"kind": "monthly"}),
])
def test_placeholder_cannot_create_price_request(model, extra):
    with pytest.raises(ValidationError, match="证券代码"):
        model(request_id="test-only", tickers=["none"], **extra)


def test_old_frozen_feed_revision_is_normalized_only_on_read():
    saved = {"ticker": "none", "company": "SYNTHETIC Bank"}
    assert _ticker_read_view(dict(saved)) == {
        "ticker": None, "company": "SYNTHETIC Bank", "issuer_ticker_raw": "none"
    }
    assert saved["ticker"] == "none"
    assert _compact_row({"ticker": "none", "shares": "10"}) == {
        "ticker": None, "shares": "10", "issuer_ticker_raw": "none"
    }
    assert _compact_row({"ticker": "AAPL"}) == {"ticker": "AAPL"}
