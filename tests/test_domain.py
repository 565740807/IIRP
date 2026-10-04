"""P0 math/security checks using synthetic data, not a full SEC coverage audit."""

from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest
from iirp.analytics.prices import (
    endpoint_change,
    event_returns,
    interval_summary,
    maximum_drawdown,
    price_path,
)
from iirp.domain.ownership import OwnershipParseError, ParseLimits, parse_ownership_xml

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_ownership_form4.xml"


def parse(content=None, **kwargs):
    return parse_ownership_xml(
        FIXTURE.read_bytes() if content is None else content,
        source_kind="synthetic",
        accession="SYNTHETIC-0001",
        **kwargs,
    )


def test_before_baseline_return_is_not_negative_of_path_point():
    assert endpoint_change(D(100), D(150)).value == D("0.5")
    old_point = price_path([D(100), D(150)], baseline=D(150))[0]
    assert old_point.quantize(D("0.0001")) == D("-0.3333")
    assert old_point != -endpoint_change(D(100), D(150)).value


def test_path_endpoint_relative_loss_and_drawdown_are_distinct():
    result = interval_summary([D(100), D(120), D(90), D(110)])
    assert result.endpoint.value == D("0.1")
    assert result.relative_high.value == D("0.2")
    assert result.relative_low.value == D("-0.1")
    assert result.closing_max_drawdown.value == D("0.25")
    assert result.complete


def test_endpoint_can_survive_gap_but_path_extremes_cannot():
    result = interval_summary([D(100), None, D(90), D(110)])
    assert result.endpoint.value == D("0.1")
    assert result.path == (D(0), None, D("-0.1"), D("0.1"))
    assert result.closing_max_drawdown.status == "incomplete_path"
    assert result.relative_high.value is None
    assert not result.complete
    assert (result.observed_sessions, result.expected_sessions) == (3, 4)


def test_missing_start_does_not_silently_move_to_next_close():
    result = interval_summary([None, D(110), D(121)])
    assert result.endpoint.status == "missing_price"
    assert result.path == (None, None, None)
    assert maximum_drawdown([None, D(110)]).value is None


def test_single_session_and_no_sessions_are_distinct():
    single = interval_summary([D(100)])
    assert single.endpoint.value == D(0)
    assert single.closing_max_drawdown.value == D(0)
    empty = interval_summary([])
    assert empty.endpoint.status == "no_sessions"
    assert empty.endpoint.value is None


def test_gap_and_after_open_compound_and_reaction_day_counts_as_one():
    result = event_returns(
        D(100),
        D(110),
        [D(112), D(113), D(115), D(118), D(121)],
        opening_attribution=True,
    )
    assert result.opening_gap.value == D("0.1")
    assert result.windows[5].after_open.value == D("0.1")
    assert result.windows[5].cumulative.value == D("0.21")
    assert result.windows[1].cumulative.value == D("0.12")
    assert result.windows[20].cumulative.status == "not_yet_formed"


def test_formed_missing_and_unconfirmed_timing_are_distinct():
    result = event_returns(D(100), D(110), [None, D(115)], opening_attribution=False)
    assert result.windows[1].cumulative.status == "missing_price"
    assert result.windows[5].cumulative.status == "not_yet_formed"
    assert result.opening_gap.status == "unconfirmed_event_time"
    assert result.windows[1].after_open.status == "unconfirmed_event_time"


@pytest.mark.parametrize("bad", [D(0), D(-1), D("NaN"), D("Infinity")])
def test_invalid_price_is_not_a_synthetic_zero_return(bad):
    with pytest.raises(ValueError):
        endpoint_change(D(100), bad)


def test_float_inputs_and_ambient_decimal_precision_cannot_change_results():
    with pytest.raises(TypeError):
        endpoint_change(100.0, D(101))
    with localcontext() as context:
        context.prec = 2
        assert endpoint_change(D(100), D(121)).value == D("0.21")


def test_joint_owners_do_not_duplicate_economic_source_row():
    filing = parse()
    assert filing.source_kind == "synthetic"
    assert filing.issuer_cik == "0000000123"
    assert len(filing.owners) == 2
    assert [owner.cik for owner in filing.owners] == ["0000000456", "0000000789"]
    assert len(filing.rows) == 3
    purchase = filing.rows[0]
    assert purchase.shares == D(100000)
    assert purchase.table == "I" and purchase.source_row_index == 1
    assert purchase.transaction_date.isoformat() == "2026-08-31"
    assert purchase.code == "P" and purchase.direction == "A"
    assert purchase.action_category == "purchase_market_or_private"
    assert purchase.footnote_ids == ("F1",)
    assert filing.footnotes[0].text.startswith("SYNTHETIC")
    assert filing.raw_xml == FIXTURE.read_bytes()
    assert len(filing.content_sha256) == 64


def test_holdings_and_derivative_actions_do_not_become_common_stock_buys():
    filing = parse()
    holding, derivative = filing.rows[1:]
    assert holding.row_kind == "holding" and not holding.is_trade_observation
    assert holding.action_category == "holding"
    assert derivative.table == "II"
    assert derivative.code == "M" and derivative.direction == "D"
    assert derivative.action_category == "exercise_or_conversion"
    assert derivative.exercise_price == D("10.50")
    assert derivative.underlying_shares == D(1000)
    assert derivative.price_per_share is None  # Never extract 10.50 from a footnote.
    assert derivative.currency is None and derivative.quantity_unit is None


def test_form3_holdings_only_and_unexpected_transaction_preserved_for_review():
    content = b"""<ownershipDocument><documentType>3/A</documentType>
      <issuer><issuerCik>123</issuerCik></issuer>
      <nonDerivativeTable><nonDerivativeHolding><securityTitle><value>Common</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>42</value>
      </sharesOwnedFollowingTransaction></postTransactionAmounts></nonDerivativeHolding>
      </nonDerivativeTable></ownershipDocument>"""
    filing = parse(content)
    assert filing.form_type == "3/A" and filing.is_amendment
    assert filing.rows[0].shares_after == D(42)
    assert filing.rows[0].transaction_date is None
    assert not filing.rows[0].is_trade_observation
    malformed = parse(FIXTURE.read_bytes().replace(b"<documentType>4</", b"<documentType>3</"))
    assert len(malformed.rows) == 3
    assert not any(row.is_trade_observation for row in malformed.rows)
    assert malformed.rows[0].action_category == "needs_review"
    assert "form3_transaction_requires_review" in malformed.rows[0].warnings


@pytest.mark.parametrize("form", ["3", "3A", "4", "4A", "5", "5A"])
def test_supported_form_types_preserve_original_form(form):
    filing = parse(
        FIXTURE.read_bytes().replace(b"<documentType>4</", f"<documentType>{form}</".encode())
    )
    assert filing.raw_document_type == form
    assert filing.form_type == form.replace("A", "/A")


def test_changed_amendment_row_remains_separate_source_observation():
    original = parse()
    amended = parse(
        FIXTURE.read_bytes()
        .replace(b"<documentType>4</", b"<documentType>4/A</")
        .replace(b"<value>100000</value>", b"<value>90000</value>")
    )
    assert original.rows[0].shares == D(100000)
    assert amended.rows[0].shares == D(90000)
    assert original.content_sha256 != amended.content_sha256
    assert amended.is_amendment
    assert amended.amendment_resolution == "unresolved"
    assert original.amendment_resolution == "not_applicable"


def test_invalid_source_fields_are_not_coerced_into_financial_facts():
    content = FIXTURE.read_bytes().replace(b"<value>100000</value>", b"<value>NaN</value>")
    content = content.replace(b"<value>2026-08-31</value>", b"<value>2026-02-30</value>")
    filing = parse(content)
    assert filing.rows[0].shares is None and filing.rows[0].transaction_date is None
    assert "invalid_number:shares" in filing.rows[0].warnings
    assert b"NaN" in filing.rows[0].raw_xml
    assert b"2026-02-30" in filing.rows[0].raw_xml


@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE ownershipDocument [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
        '<!DOCTYPE ownershipDocument [<!ENTITY x "small"> <!ENTITY y "&x;&x;&x;">]>',
        '<!DOCTYPE ownershipDocument SYSTEM "https://example.invalid/external.dtd">',
    ],
)
def test_entities_and_dtd_are_rejected_before_resolution(declaration):
    with pytest.raises(OwnershipParseError, match="Unsafe or malformed XML"):
        parse(
            (
                declaration
                + "<ownershipDocument><documentType>4</documentType></ownershipDocument>"
            ).encode()
        )


def test_size_rows_depth_and_node_count_are_bounded():
    for limits in [
        ParseLimits(max_bytes=50),
        ParseLimits(max_rows=1),
        ParseLimits(max_depth=2),
        ParseLimits(max_nodes=3),
    ]:
        with pytest.raises(OwnershipParseError, match="limit"):
            parse(limits=limits)
    with pytest.raises(OwnershipParseError, match="ownershipDocument"):
        parse(b"<html>not an SEC ownership document</html>")


@pytest.mark.parametrize("raw", [b"1_000", b"1e3", b"-1", b"Infinity"])
def test_non_xml_decimal_values_preserve_raw_without_numeric_coercion(raw):
    filing = parse(
        FIXTURE.read_bytes().replace(b"<value>100000</value>", b"<value>" + raw + b"</value>")
    )
    assert filing.rows[0].shares is None
    assert "invalid_number:shares" in filing.rows[0].warnings


def test_namespace_and_unknown_fields_are_preserved_without_guessing():
    content = (
        FIXTURE.read_bytes()
        .replace(
            b"<ownershipDocument>",
            b'<ownershipDocument xmlns="urn:synthetic-ownership">',
        )
        .replace(
            b"</nonDerivativeTable>", b"<unknownRow>ambiguous raw</unknownRow></nonDerivativeTable>"
        )
    )
    filing = parse(content)
    assert filing.issuer_cik == "0000000123"
    assert len(filing.rows) == 3
    assert "unrecognized_table_child:I:3" in filing.warnings
    assert b"ambiguous raw" in filing.raw_xml


def test_code_direction_conflict_is_marked_for_review():
    content = FIXTURE.read_bytes().replace(
        b"<transactionAcquiredDisposedCode><value>A</value>",
        b"<transactionAcquiredDisposedCode><value>D</value>",
    )
    row = parse(content).rows[0]
    assert row.code == "P" and row.direction == "D"
    assert row.action_category == "needs_review"
    assert "code_direction_conflict" in row.warnings


def test_official_form4_grant_and_holding_sample():
    """One real SEC file guards against treating all acquisitions as P purchases."""
    from pathlib import Path

    from iirp.domain.ownership import parse_ownership_xml

    raw = (Path(__file__).parent / "fixtures/sec_form4_0001493152-26-041638.xml").read_bytes()
    filing = parse_ownership_xml(raw, source_kind="sec", accession="0001493152-26-041638")
    assert (
        filing.content_sha256 == "137d3059e1baf34d58b39eac18118e2f99c96e110bcd11fde9c0b515698ffdbc"
    )
    assert filing.issuer_cik == "0001702924"
    assert len(filing.owners) == 1
    assert len(filing.rows) == 2
    assert [row.action_category for row in filing.rows] == ["grant_or_award", "holding"]
    assert sum(row.is_trade_observation for row in filing.rows) == 1
