"""Research inputs depend on the price cache that answers them (synthetic, real DB)."""
from datetime import date

from iirp import lifecycle
from iirp.business_models import AnalysisRequest, PriceCache, Security
from iirp.db import session
from test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_database,
    refetch_prices,
    seed_prices,
    seed_security,
)
from test_performance_pipeline import params


def _key(request_id, cache_id, security_id):
    with session() as s:
        return lifecycle.research_input_key(s.get(AnalysisRequest, request_id), s.get(PriceCache, cache_id), [], s.get(Security, security_id))


def test_same_cache_gives_the_same_input_and_a_refetch_a_new_one():
    security = seed_security()
    old = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5), wide=True)
    research = lifecycle.create_analysis(params(kind="interval", start_mmdd="01-03", end_mmdd="01-05"))
    before = _key(research["id"], old, security)
    assert _key(research["id"], old, security) == before
    latest = refetch_prices(security, date(2023, 6, 1), date(2023, 6, 2))
    assert latest != old
    assert _key(research["id"], latest, security) != before


def test_dependency_records_the_cache_and_rows_in_range():
    from iirp.research_dependencies import dataset_dependency

    security = seed_security()
    cache_id = seed_prices(security, date(2023, 1, 3), date(2023, 1, 5))
    with session() as s:
        dependency = dataset_dependency(s, s.get(PriceCache, cache_id), [(date(2023, 1, 4), date(2023, 1, 5))])
    assert dependency["cache_id"] == cache_id and dependency["row_count"] == 2
    assert dependency["fetched_at"]
