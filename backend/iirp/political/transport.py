"""Contact-bearing requests serialized across processes, without open SQL transactions."""
import time
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlsplit

import httpx
import psycopg
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from iirp.config import settings
from iirp.db import session
from iirp.jobs.operations import ProviderFailure
from iirp.messages import msg
from iirp.models import SourceBudget, now
from iirp.political.config import policy
from iirp.sec import contact

ALLOWED_HOSTS = {
    "disclosures-clerk.house.gov", "efdsearch.senate.gov", "www.oge.gov",
    "extapps2.oge.gov", "oge.gov", "www2.oge.gov", "oge.box.com",
    "www.whitehouse.gov", "whitehouse.gov",
}


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError(msg("political.invalid_source_url"))
    return parsed.hostname


class Transport:
    def __init__(self):
        self.client = httpx.Client(
            timeout=httpx.Timeout(policy()["collection"]["request_timeout_seconds"],
                                  connect=policy()["collection"]["connect_timeout_seconds"]),
            follow_redirects=False,
            headers={"User-Agent": contact.user_agent()},
        )

    def close(self):
        self.client.close()

    def fetch(self, method, url, data=None):
        host = validate_url(url)
        if not contact.configured():
            raise ProviderFailure(msg("political.contact_required"), 900, source_wait=True)
        provider = "political_" + ("house" if host == "disclosures-clerk.house.gov" else
                                  "senate" if host == "efdsearch.senate.gov" else "oge")
        key = int(sha256(provider.encode()).hexdigest()[:15], 16)
        dburl = make_url(settings().database_url)
        with psycopg.connect(host=dburl.host, port=dburl.port, user=dburl.username,
                             password=dburl.password, dbname=dburl.database, autocommit=True) as lock:
            if not lock.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
                raise ProviderFailure(msg("political.source_busy"), 2, source_wait=True)
            try:
                with session() as s, s.begin():
                    s.execute(insert(SourceBudget).values(provider=provider).on_conflict_do_nothing())
                    budget = s.get(SourceBudget, provider, with_for_update=True)
                    wait = (budget.next_allowed_at - now()).total_seconds()
                if wait > 2:
                    raise ProviderFailure(msg("political.cooldown"), wait, source_wait=True)
                if wait > 0:
                    time.sleep(wait)
                try:
                    with self.client.stream(method, url, data=data, headers={"Referer":url}) as response:
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > policy()["collection"]["max_file_bytes"]:
                                raise ProviderFailure(msg("political.file_too_large"), 900)
                            chunks.append(chunk)
                        headers = {key:value for key,value in response.headers.items()
                                   if key.lower() not in ("content-encoding", "content-length")}
                        result = httpx.Response(response.status_code, headers=headers,
                                                content=b"".join(chunks), request=response.request)
                    if result.status_code >= 400:
                        raise ProviderFailure(msg("political.http_unavailable", status=result.status_code),
                                              900, result.status_code, source_wait=True)
                except Exception:
                    with session() as s, s.begin():
                        budget = s.get(SourceBudget, provider, with_for_update=True)
                        budget.failures += 1
                        delay = min(policy()["cadence"]["retry_max_seconds"],
                                    900 * 2 ** min(budget.failures - 1, 7))
                        budget.next_allowed_at = now() + timedelta(seconds=delay)
                    raise
                with session() as s, s.begin():
                    budget = s.get(SourceBudget, provider, with_for_update=True)
                    budget.next_allowed_at = now() + timedelta(seconds=policy()["collection"]["request_gap_seconds"])
                    budget.failures = 0
                return result
            finally:
                lock.execute("SELECT pg_advisory_unlock(%s)", (key,))
