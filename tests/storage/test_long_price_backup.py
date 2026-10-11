"""Backups retain price retention policy and schema, and omit downloadable bars."""
from datetime import date

from iirp.db import session
from iirp.market.long_prices import has_bars
from iirp.models import LongPriceBar, LongPriceSeries, Security
from sqlalchemy import select

from tests.storage.test_maintenance_lifecycle import isolated as isolated


def test_backup_excludes_history_and_restore_verifies_retention_frontier(isolated):
    with session() as s, s.begin():
        security = Security(symbol="SYNTH", status="VERIFIED", instrument="EQUITY", calendar="XNYS", currency="USD")
        s.add(security)
        s.flush()
        row = LongPriceSeries(symbol="SYNTH", security_id=security.id, retain_from=date(2026, 7, 1),
            start_date=date(2026, 7, 1), end_date=date(2026, 10, 9), complete_through=date(2026, 10, 9))
        s.add(row)
        s.flush()
        s.add(LongPriceBar(cache_id=row.id, session_date=date(2026, 10, 9),
            open=100, high=100, low=100, close=100, status="VALID"))
    directory = isolated.backup.backup()
    report = isolated.backup.restore_verify(directory, discard=False)
    with isolated.backup.connection(report["database"]) as conn:
        assert conn.execute("SELECT count(*) FROM long_price_bar").fetchone() == (0,)
        assert conn.execute("SELECT retain_from FROM long_price_series").fetchone() == (date(2026, 7, 1),)
        assert conn.execute("SELECT to_regclass('long_price_bar') IS NOT NULL").fetchone() == (True,)
    with session() as s:
        assert has_bars(s, s.scalar(select(LongPriceSeries)))
    assert report["fact_hashes_verified"] > 20
