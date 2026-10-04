"""Measure actual HTTP calls separately from Yahoo adapter and local-cache time."""

import time
from functools import lru_cache
from urllib.parse import urlsplit


class HttpTiming:
    def __init__(self, session):
        self.session = session
        self.original_request = session.request
        session.request = self.request
        self.reset()

    def reset(self):
        self.seconds = 0.0
        self.count = 0
        self.observations = []

    def request(self, method, url, *args, **kwargs):
        started = time.perf_counter()
        status = None
        try:
            response = self.original_request(method, url, *args, **kwargs)
            status = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            self.seconds += elapsed
            self.count += 1
            if len(self.observations) < 100:
                # Host only: cookies, query credentials and response bodies never
                # belong in diagnostic timing metadata.
                self.observations.append({"host": urlsplit(url).hostname,
                                          "status": status, "seconds": elapsed})

    def snapshot(self):
        return {"http_seconds": self.seconds, "http_requests": self.count,
                "http_observations": list(self.observations)}


@lru_cache(maxsize=1)
def timed_session():
    # Use the pinned provider's default factory so TLS, proxy and fallback
    # behavior are identical. Ticker's public session parameter supplies it.
    from yfinance._http import new_session

    return HttpTiming(new_session())
