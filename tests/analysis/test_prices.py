"""Price-change helpers reject invalid prices and floats; precision is fixed."""

from decimal import Decimal as D
from decimal import localcontext

import pytest
from iirp.analysis.prices import endpoint_change


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
