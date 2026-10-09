"""Key trades (focus) are open-market purchases and sales only; migration 0030.

Real PostgreSQL in a disposable iirp_v1_test_* database; all filings are synthetic.
"""


from iirp.insider.views import _matches

from tests.sec.test_sec_facts import clean, isolated_database  # noqa: F401


def test_focus_is_table_one_purchases_and_sales():
    assert _matches({"table": "I", "code": "P"}, "focus") and _matches({"table": "I", "code": "S"}, "focus")
    # Grants, exercises and derivative rows stay under "all".
    for row in ({"table": "I", "code": "A"}, {"table": "I", "code": "M"},
                {"table": "II", "code": "M"}, {"table": "II", "code": "A"}):
        assert not _matches(row, "focus") and _matches(row, "all")
    assert _matches({"table": "II", "code": "M"}, "derivative")
