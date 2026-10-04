"""Feed regression and optional representative isolated-database benchmark.

Enable IIRP_RUN_LARGE_PERFORMANCE=1 for 100k synthetic filings, 500k transactions,
100 securities with nine years of synthetic prices, and 50 inert mixed jobs.
No provider requests, live database writes, or fabricated live coverage occur.
"""

import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import date, timedelta

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from iirp.analytics.calendar import sessions
from iirp.business_models import FeedRevision, FeedSession
from iirp.config import ROOT, settings
from iirp.db import engine, session
from iirp.feed_index import reconcile
from iirp.sec_facts import feed, feed_group, latest_feed_metadata
from psycopg import sql
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

LARGE = os.environ.get("IIRP_RUN_LARGE_PERFORMANCE") == "1"
GROUPS = 5000 if LARGE else 60


@pytest.fixture(scope="module", autouse=True)
def isolated_performance_database(tmp_path_factory):
    url = make_url(settings().database_url)
    name = "iirp_v1_test_feed_perf_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname="postgres",
        autocommit=True,
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous_runtime = os.environ.get("IIRP_RUNTIME_DIR")
    runtime = tmp_path_factory.mktemp("isolated-feed-performance")
    os.environ["IIRP_RUNTIME_DIR"] = str(runtime)
    previous = os.environ.get("IIRP_DATABASE_URL")
    os.environ["IIRP_DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
    engine.cache_clear()
    settings.cache_clear()
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        seed_dataset(GROUPS)
        yield {"database": name, "runtime": runtime}
    finally:
        engine().dispose()
        engine.cache_clear()
        if previous is None:
            os.environ.pop("IIRP_DATABASE_URL", None)
        else:
            os.environ["IIRP_DATABASE_URL"] = previous
        if previous_runtime is None:
            os.environ.pop("IIRP_RUNTIME_DIR", None)
        else:
            os.environ["IIRP_RUNTIME_DIR"] = previous_runtime
        settings.cache_clear()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def seed_dataset(groups):
    with engine().begin() as c:
        c.execute(text("SET LOCAL statement_timeout = '120s'"))
        c.execute(
            text(
                "INSERT INTO source_object (sha256,relative_path,byte_size,media_type,created_at) VALUES (:hash,'synthetic/performance',0,'application/synthetic',now())"
            ),
            {"hash": "a" * 64},
        )
        c.execute(
            text(
                "INSERT INTO issuer (id,name) SELECT lpad(i::text,10,'0'), 'SYNTHETIC issuer '||i FROM generate_series(1,100) i"
            )
        )
        c.execute(
            text("INSERT INTO reporting_owner (id,name) VALUES ('0000000999','SYNTHETIC owner')")
        )
        c.execute(
            text("""INSERT INTO security (id,symbol,name,issuer_id,instrument,currency,exchange,calendar,status,metadata_json,maintain)
            SELECT md5('security'||i),'SYN'||i,'SYNTHETIC security '||i,lpad(i::text,10,'0'),
            'EQUITY','USD','NMS','XNYS','VERIFIED','{"synthetic": true}',false FROM generate_series(1,100) i""")
        )
        c.execute(
            text("""INSERT INTO filing (accession,form,filing_date,accepted_at,first_seen_at,issuer_id,index_url,status,current_version,visible)
            SELECT '0000000001-25-'||lpad(i::text,6,'0'),'4',CAST(:base_day AS date)+((i-1)/2000),
            ((CAST(:base_day AS date)+((i-1)/2000))+time '18:00') AT TIME ZONE 'America/New_York',now(),
            lpad((1+((i-1)/20)%100)::text,10,'0'),'https://example.invalid/synthetic','PARSED',md5('version'||i),true
            FROM generate_series(1,:n) i"""),
            {"n": groups * 20, "base_day": str(date.today() - timedelta(days=50))},
        )
        c.execute(
            text("""INSERT INTO filing_version (id,accession,source_hash,parser_version,data,created_at)
            SELECT current_version,accession,:hash,'synthetic-performance','{"synthetic": true}',now() FROM filing"""),
            {"hash": "a" * 64},
        )
        c.execute(
            text("""INSERT INTO transaction_event (id,issuer_id,accession,version_id,row_key,transaction_date,accepted_at,data,owner_ids,status)
            SELECT md5('event'||f.accession||r), f.issuer_id,f.accession,f.current_version,'I:transaction:'||r,
            f.filing_date,f.accepted_at,jsonb_build_object(
            'synthetic',true,'table','I','code','P','security_title','SYNTHETIC Common Stock','currency','USD',
            'kind','公开市场或私人买入','action_category','purchase_market_or_private','shares','100','price_per_share','10','raw_xml',repeat('synthetic-xml-',80)),
            '["0000000999"]','CURRENT' FROM filing f CROSS JOIN generate_series(1,5) r""")
        )
        # These are explicit generated immutable observations, not source XML.
        c.execute(
            text("""UPDATE filing_version v SET data = jsonb_build_object(
            'synthetic',true,'issuer_cik',f.issuer_id,'issuer_name','SYNTHETIC issuer '||f.issuer_id,
            'issuer_ticker','SYN','form_type','4','raw_10b5_1_flag',NULL,
            'source_metadata',jsonb_build_object('accepted_at',f.accepted_at),
            'owners',jsonb_build_array(jsonb_build_object('cik','0000000999','name','SYNTHETIC owner')),
            'rows',(SELECT jsonb_agg(jsonb_build_object('table','I','row_kind','transaction',
                'source_row_index',r,'is_trade_observation',true,'action_category','purchase_market_or_private',
                'security_title','SYNTHETIC Common Stock','transaction_date',f.filing_date,'code','P',
                'shares','100','price_per_share','10','currency','USD','quantity_unit','shares',
                'footnote_ids','[]'::jsonb,'warnings','[]'::jsonb)) FROM generate_series(1,5) r))
            FROM filing f WHERE f.current_version=v.id""")
        )
        c.execute(
            text("""INSERT INTO feed_group_revision (id,group_key,issuer_id,accepted_at,data,created_at,match_kinds,row_count)
            SELECT md5('revision'||issuer_id||filing_date),md5('group'||issuer_id||filing_date),issuer_id,max(accepted_at),
            jsonb_build_object('id',md5('group'||issuer_id||filing_date),'issuer_id',issuer_id,
            'company','SYNTHETIC issuer '||issuer_id,'ticker','SYN','accepted_at',max(accepted_at),
            'accepted_date',filing_date,'transaction_dates',jsonb_build_array(filing_date),'owners',1,'filings',20,
            'total_transactions',count(*),'transactions',jsonb_agg(
                (data-'raw_xml')||jsonb_build_object('id',id,'issuer_id',issuer_id,'version_id',version_id,'accession',accession,
                'transaction_date',transaction_date,'accepted_at',accepted_at,'owner_ids',owner_ids,'owner_id','0000000999',
                'owner','SYNTHETIC owner','status','CURRENT','eligible_for_totals',true,'price','10','amount','1000','known_amount','1000') ORDER BY id),
            'summary','[]'::jsonb,'amendment_updates',0),now(),'["all","focus","buy"]',count(*)
            FROM (SELECT t.*,f.filing_date FROM transaction_event t JOIN filing f USING(accession)) grouped
            GROUP BY issuer_id,filing_date""")
        )
        c.execute(
            text("""INSERT INTO job (id,kind,title,target,idempotency_key,status,trigger,priority,requested_action,control_version,progress_done,progress_total,checkpoint,attempts,available_at,created_at,updated_at)
            SELECT md5('job'||i),(ARRAY['market_history','sec_document','earnings_candidates','research_compute','maintenance'])[1+(i%5)],
            'SYNTHETIC inert mixed job','{"synthetic": true}',md5('job-key'||i),
            CASE WHEN i%3=0 THEN 'PAUSED' ELSE 'QUEUED' END,'manual',10,NULL,0,0,1,'{}',0,now(),now(),now()
            FROM generate_series(1,50) i""")
        )
        if LARGE:
            days = [str(day) for day in sessions(date(2017, 1, 1), date(2025, 12, 31))]
            c.execute(text("CREATE TEMP TABLE synthetic_days (day date PRIMARY KEY)"))
            c.execute(
                text("INSERT INTO synthetic_days VALUES (:day)"), [{"day": day} for day in days]
            )
            c.execute(
                text("""INSERT INTO price_dataset_version (id,security_id,basis,basis_key,status,manifest,created_at,published_at)
                SELECT md5('dataset'||id),id,'SPLIT_ONLY','synthetic-performance','PUBLISHED','{"synthetic": true}',now(),now() FROM security""")
            )
            c.execute(
                text("""INSERT INTO market_bar_revision (id,security_id,session_date,provider,source_hash,record_hash,open,high,low,close,adj_close,volume,status,reason,observed_at)
                SELECT md5(s.id||d.day),s.id,d.day,'synthetic-performance',:hash,md5(s.id||d.day),100,110,90,101,101,1000,'VALID','synthetic benchmark only',now()
                FROM security s CROSS JOIN synthetic_days d"""),
                {"hash": "a" * 64},
            )
            c.execute(
                text("""INSERT INTO dataset_bar (dataset_id,session_date,bar_id)
                SELECT md5('dataset'||security_id),session_date,id FROM market_bar_revision""")
            )
    with session() as s, s.begin():
        # Synthetic revisions were inserted directly; publish their pointers.
        s.execute(text("SET LOCAL statement_timeout = '120s'"))
        reconcile(s)
    with engine().begin() as c:
        # Dataset preparation retains the seed phase's bounded budget. Actual
        # application reads still use their normal 5-second statement deadline.
        c.execute(text("SET LOCAL statement_timeout = '120s'"))
        c.execute(text("ANALYZE"))


def test_feed_only_materializes_current_page_and_preserves_whole_group_totals():
    loaded = []

    def record(session, instance):
        if isinstance(instance, FeedRevision):
            loaded.append(instance.id)

    sa_event.listen(Session, "loaded_as_persistent", record)
    try:
        with session() as s, s.begin():
            result = feed(s, kind="buy")
            assert len(loaded) == 20
            assert result["total_groups"] == GROUPS
            assert len(result["groups"]) == 20
            group = result["groups"][0]
            assert len(group["transactions"]) == 20
            assert group["matching_transactions"] == 100
            assert group["summary"][0]["known_amount"] == "100000"
            assert "raw_xml" not in json.dumps(result)
            saved = s.get(FeedSession, result["session_id"])
            # A reading session is a watermark: no revision list, no manifest.
            assert "rows" not in saved.filters and saved.revision_ids == []
            assert saved.manifest_hash is None and saved.filters["total_groups"] == GROUPS
            detail = feed_group(s, saved.id, group["id"], cursor="20")
            assert len(detail["items"]) == 20 and detail["total"] == 100
            assert not (
                {row["id"] for row in group["transactions"]}
                & {row["id"] for row in detail["items"]}
            )
    finally:
        sa_event.remove(Session, "loaded_as_persistent", record)


def test_metadata_filter_does_not_load_revision_payloads():
    with session() as s:
        result = latest_feed_metadata(s, "purchase")
        assert len(result) == GROUPS
        assert set(result[0]) == {"id", "group_key", "accepted_at"}
        assert not list(s.identity_map.values())
        assert latest_feed_metadata(s, "derivative") == []


SAMPLES = int(os.environ.get("IIRP_PERFORMANCE_SAMPLES", "100"))


def percentiles(samples):
    samples = sorted(samples)
    return {
        "samples": len(samples),
        "p50_ms": round((samples[(len(samples) - 1) // 2] + samples[len(samples) // 2]) / 2, 3),
        "p95_ms": round(samples[math.ceil(len(samples) * 0.95) - 1], 3),
        "max_ms": round(samples[-1], 3),
    }


@pytest.mark.skipif(not LARGE, reason="Explicit optional representative synthetic workload")
def test_large_api_latency_with_frozen_session_new_count():
    from iirp.analytics.research import compute_research
    from iirp.api import app
    from iirp.business_models import Security
    from iirp.market_data import price_bars

    timings, sizes, first_requests, entity_sessions = {}, {}, {}, {}
    with TestClient(app, headers={"X-IIRP-Client": "web"}) as client:
        baseline = client.get("/api/v1/feed?type=buy")
        assert baseline.status_code == 200
        saved_id = baseline.json()["session_id"]
        cursor = baseline.json()["next_cursor"]
        for _ in range(13):  # Keyset cursors are opaque: walk to page 15.
            cursor = client.get("/api/v1/feed", params={"type": "buy", "session_id": saved_id,
                                                        "cursor": cursor}).json()["next_cursor"]
        endpoints = {
            "fresh_feed": "/api/v1/feed?type=buy",
            "frozen_feed_with_new_count": "/api/v1/feed?type=buy&session_id=" + saved_id,
            "frozen_page_15": "/api/v1/feed?type=buy&session_id=" + saved_id + "&cursor=" + cursor,
            "home": "/api/v1/home",
            "company_recent50": "/api/v1/companies/0000000001?recent_count=50",
            "person_recent50": "/api/v1/people/0000000999?recent_count=50",
            "company_three_month_fresh": "/api/v1/companies/0000000001",
            "person_three_month_fresh": "/api/v1/people/0000000999",
        }
        for label, path in list(endpoints.items()):
            started = time.perf_counter()
            initial = client.get(path)
            first_requests[label] = round((time.perf_counter() - started) * 1000, 3)
            print("initial", label, first_requests[label], initial.status_code, flush=True)
            assert initial.status_code == 200, (label, initial.text)
            if "three_month_fresh" in label:
                identifier = initial.json()["data"]["session_id"]
                entity_sessions[label] = identifier
                endpoints[label.replace("fresh", "frozen")] = path + "?cursor=" + identifier + ":0"
        for label, path in endpoints.items():
            values = []
            for index in range(SAMPLES):
                started = time.perf_counter()
                response = client.get(path)
                elapsed = (time.perf_counter() - started) * 1000
                assert response.status_code == 200, (label, response.text)
                body = response.json()
                if label.startswith("frozen"):
                    assert body["new_count"] == 0
                if label.startswith(("company", "person")):
                    assert len(body["items"]) == 50
                values.append(elapsed)
                sizes[label] = len(response.content)
                identifier = body.get("session_id") or body.get("data", {}).get("session_id")
                if identifier and "frozen" not in label:
                    # Remove only this request's disposable snapshot, outside timing.
                    with session() as db, db.begin():
                        db.execute(
                            text("DELETE FROM feed_session WHERE id=:id"), {"id": identifier}
                        )
                if label == "person_three_month_fresh" and index % 5 == 4:
                    with (
                        engine()
                        .connect()
                        .execution_options(isolation_level="AUTOCOMMIT") as connection
                    ):
                        connection.execute(text("VACUUM (SKIP_LOCKED, TRUNCATE FALSE) feed_session"))
            timings[label] = percentiles(values)
            print(label, timings[label], flush=True)
    with session() as s:
        security = s.scalar(select(Security).where(Security.symbol == "SYN1"))
        bars, dataset = price_bars(s, security.id)
    params = {
        "kind": "monthly",
        "historical_years": 8,
        "current_year": 2025,
        "month": 6,
        "comparison": "complete",
    }
    first = time.perf_counter()
    result = compute_research(params, bars, today=date(2025, 12, 31))
    first_requests["single_security_nine_year_compute"] = round(
        (time.perf_counter() - first) * 1000, 3
    )
    assert result["effective_n"] == 8
    values = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        compute_research(params, bars, today=date(2025, 12, 31))
        values.append((time.perf_counter() - started) * 1000)
    timings["single_security_nine_year_compute"] = percentiles(values)
    values = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        with session() as s:
            current_bars, _dataset = price_bars(s, security.id, dataset.id)
        compute_research(params, current_bars, today=date(2025, 12, 31))
        values.append((time.perf_counter() - started) * 1000)
    timings["nine_year_db_read_and_compute"] = percentiles(values)
    with session() as s:
        counts = {
            name: s.scalar(text("SELECT count(*) FROM " + name))
            for name in (
                "filing",
                "transaction_event",
                "security",
                "market_bar_revision",
                "feed_group_revision",
                "job",
            )
        }
        database_bytes = s.scalar(select(func.pg_database_size(func.current_database())))
    report = {
        "synthetic": True,
        "network_requests": 0,
        "method": f"FastAPI TestClient including middleware, PostgreSQL, response validation and JSON encoding; {SAMPLES} requests per endpoint; first requests separate; source database buffer cache not flushed",
        "counts": counts,
        "database_bytes": database_bytes,
        "timings": timings,
        "first_request_ms": first_requests,
        "response_bytes": sizes,
        "calculation_input": {
            "sessions": len(bars),
            "start": bars[0]["date"],
            "end": bars[-1]["date"],
            "params": params,
        },
        "claim_boundary": "Generated isolated data and 50 inert jobs; no actual worker/backfill concurrency; browser timings recorded separately",
    }
    destination = (
        ROOT
        / "runtime"
        / "validation"
        / (
            "sec-feed-performance.json"
            if SAMPLES >= 100
            else "sec-feed-performance-diagnostic.json"
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert timings["fresh_feed"]["p95_ms"] < 600
    assert timings["frozen_feed_with_new_count"]["p95_ms"] < 600
    assert timings["home"]["p95_ms"] < 250
    assert timings["single_security_nine_year_compute"]["p95_ms"] < 2000


@pytest.mark.skipif(
    not LARGE or os.environ.get("IIRP_RUN_LARGE_BROWSER") != "1",
    reason="Explicit optional isolated real-browser benchmark",
)
def test_large_browser_local_pages_and_300_groups(isolated_performance_database):
    """Use the current dist against this generated DB; never start a worker."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    output = (
        ROOT
        / "runtime"
        / "validation"
        / ("large-browser-performance" if SAMPLES >= 100 else "large-browser-diagnostic")
    )
    output.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "IIRP_PORT": str(port),
        "IIRP_SEC_USER_AGENT": "",
        "IIRP_UI_URL": base,
        "IIRP_UI_OUTPUT": str(output),
        "IIRP_UI_SYNTHETIC": "1",
        "IIRP_VALIDATION_ID": uuid.uuid4().hex,
        "IIRP_BROWSER_MANIFEST": str(output / "fixture.json"),
    }
    assert (
        make_url(environment["IIRP_DATABASE_URL"]).database
        == isolated_performance_database["database"]
    )
    (output / "fixture.json").write_text(json.dumps({
        "base": base, "database": isolated_performance_database["database"],
        "validation_id": environment["IIRP_VALIDATION_ID"],
        "synthetic": True, "no_external_provider_calls": True,
    }))
    with (output / "web.log").open("w") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.validation.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            with httpx.Client(base_url=base, timeout=1) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        response = client.get("/health/ready")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert server.poll() is None, "Isolated web exited before readiness"
                    assert time.monotonic() < deadline, "Isolated web readiness timed out"
                    time.sleep(0.1)
            subprocess.run(
                [
                    os.environ.get("IIRP_NODE", "node"),
                    str(ROOT / "browser" / "tests" / "large-performance.cjs"),
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                timeout=1200,
            )
        finally:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=5)
