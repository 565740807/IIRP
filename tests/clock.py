"""Set the clock seen by batch, planning and analysis-request code in a test."""

from iirp.analysis import requests
from iirp.insider import transactions
from iirp.jobs import batch_views, batches, planner, preferences
from iirp.market import reads

MODULES = (batches, batch_views, planner, preferences, requests, reads, transactions)


def set_clock(monkeypatch, clock):
    for module in MODULES:
        monkeypatch.setattr(module, "now", clock)
