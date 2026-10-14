"""Notifications: settings, new trades of followed people, amendments, no backlog, rule
thresholds and scope, and the ntfy sender (retries, digests, rate limits) with a fake
transport; nothing reaches the network.

Uses a disposable iirp_v1_test_* database.
"""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from iirp.db import session
from iirp.jobs.preferences import get_preferences
from iirp.jobs.queue import claim
from iirp.models import IndexConstituent, Job, NotablePerson, WatchlistItem
from iirp.models.notifications import Notification, NotificationPush
from iirp.notifications import push, settings
from iirp.notifications.scan import scan
from sqlalchemy import func, select, update

from tests.jobs.test_lifecycle import (  # noqa: F401
    clean_lifecycle,
    lifecycle_client,
    lifecycle_database,
)
from tests.notable.fixtures import politician, report, sec_trade

START = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
AT = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("tests never reach the network")

    # The real network transport; the test client's own transport stays in process.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


def turn_on(*, ntfy=False, at=START, **rules):
    with session() as s, s.begin():
        settings.update(s, {"enabled": True, "rules": rules, "ntfy": {"enabled": ntfy}}, at=at)


def follow(key, added=START - timedelta(days=30)):
    with session() as s, s.begin():
        s.add(NotablePerson(key=key, source=key.split(":")[0], name=key, added_at=added))


def run_scan(at=AT):
    with session() as s, s.begin():
        return scan(s, at=at)


def notifications(kind=None):
    with session() as s:
        query = select(Notification).order_by(Notification.created_at, Notification.dedupe_key)
        if kind:
            query = query.where(Notification.kind == kind)
        return s.scalars(query).all()


def test_settings_start_off_make_a_long_topic_and_stay_out_of_preferences(lifecycle_client):  # noqa: F811
    current = lifecycle_client.get("/api/v1/notifications/settings").json()
    assert (current["enabled"], current["ntfy"]["enabled"], current["ntfy"]["server"]) == (False, False, "https://ntfy.sh")
    assert current["ntfy"]["topic"] is None and current["rules"]["executive_min"] == 100000
    changed = lifecycle_client.put("/api/v1/notifications/settings", json={"enabled": True, "ntfy": {"enabled": True}}).json()
    topic = changed["ntfy"]["topic"]
    assert changed["enabled"] is True and changed["enabled_at"] and len(topic) >= 30
    # Turning pushes off and on keeps the topic; making a new one replaces it.
    lifecycle_client.put("/api/v1/notifications/settings", json={"ntfy": {"enabled": False}})
    again = lifecycle_client.put("/api/v1/notifications/settings", json={"ntfy": {"enabled": True}}).json()
    assert again["ntfy"]["topic"] == topic
    fresh = lifecycle_client.post("/api/v1/notifications/settings/topic").json()["ntfy"]["topic"]
    assert fresh != topic and len(fresh) >= 30
    refused = lifecycle_client.put("/api/v1/notifications/settings", json={"ntfy": {"server": "ntfy.example"}})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "notifications.server_invalid"
    assert lifecycle_client.put("/api/v1/notifications/settings", json={"rules": {"cluster_people": 1}}).status_code == 422
    saved = lifecycle_client.put("/api/v1/notifications/settings", json={"ntfy": {"server": "https://push.example.org/"}}).json()
    assert saved["ntfy"]["server"] == "https://push.example.org"
    assert "notifications" not in get_preferences()["values"]


def test_nothing_is_notified_while_off():
    follow("sec:0001767470")
    with session() as s, s.begin():
        sec_trade(s, accepted=AT - timedelta(hours=1))
    assert run_scan() == 0


def test_a_followed_filers_new_filing_is_notified_once():
    follow("sec:0001767470")
    turn_on()
    with session() as s, s.begin():
        accession = "0001767470-26-000001"
        sec_trade(s, code="P", accepted=AT - timedelta(hours=2), accession=accession)
        sec_trade(s, code="S", accepted=AT - timedelta(hours=2), accession=accession, plan=True, shares="10")
        # Accepted before notifications were turned on: history, never notified.
        sec_trade(s, code="P", accepted=START - timedelta(days=1))
        # Someone not followed.
        sec_trade(s, owner="0000000099", owner_name="Other", accepted=AT - timedelta(hours=1))
    assert run_scan() == 1
    # Reading again, or later, adds nothing.
    assert run_scan() == 0 and run_scan(AT + timedelta(minutes=5)) == 0
    [item] = notifications()
    data = item.data
    assert (item.kind, item.dedupe_key, item.person_key) == ("trade", "trade:sec:0001767470-26-000001", "sec:0001767470")
    assert (data["buy"], data["sell"]) == ({"rows": 1, "amount": "25000.00"}, {"rows": 1, "amount": "250.00", "plan": "yes"})
    assert data["person"]["name"] == "Ryan Cohen" and data["person"]["roles"] == ["ceo", "director"]
    assert data["url"].startswith("https://www.sec.gov/") and data["amendment"] is False
    assert item.push_state is None


def test_amendment_is_marked_and_not_counted_by_rules():
    follow("sec:0001767470")
    turn_on()
    with session() as s, s.begin():
        sec_trade(s, code="P", accepted=AT - timedelta(hours=2), form="4/A", shares="100000")
    assert run_scan() == 1
    [item] = notifications()
    assert item.data["amendment"] is True and item.kind == "trade"
    assert notifications("rule_executive") == []


def test_trades_before_a_person_was_added_are_not_notified():
    follow("sec:0001767470", added=AT - timedelta(hours=1))
    turn_on()
    with session() as s, s.begin():
        sec_trade(s, accepted=AT - timedelta(hours=3))
    assert run_scan() == 0
    with session() as s, s.begin():
        sec_trade(s, accepted=AT - timedelta(minutes=30))
    assert run_scan() == 1


def test_a_report_is_notified_once_when_read_and_not_for_backlog():
    follow("house:Nancy Pelosi|CA11")
    turn_on()
    with session() as s, s.begin():
        pelosi = politician(s)
        item = report(s, pelosi, report_date=date(2026, 10, 7), discovered_at=AT - timedelta(hours=1), rows=[
            ("NVDA", "NVIDIA Corporation - Common Stock", "purchase", date(2026, 9, 20), "1000001", "5000000"),
            ("AAPL", "Apple Inc. - Common Stock", "sale", date(2026, 9, 18), "500001", "1000000", ["ocr"]),
            (None, "US TREASURY BILL bond", "purchase", date(2026, 9, 19), "1001", "15000"),
        ])
        report_id = item.id
        # Still being read: not yet.
        item.pages_done, item.page_count = 1, 2
        # A first collection finding an old report after notifications were turned on.
        report(s, pelosi, report_date=date(2026, 8, 1), discovered_at=AT - timedelta(hours=1),
               rows=[("MSFT", "Microsoft", "purchase", date(2026, 7, 20), "1001", "15000")])
    assert run_scan() == 0
    with session() as s, s.begin():
        s.execute(update(item.__class__).where(item.__class__.id == report_id).values(pages_done=2))
    assert run_scan() == 1
    # Read again (a new parser version): still once.
    assert run_scan() == 0
    [notified] = notifications()
    assert notified.dedupe_key == f"trade:political:{report_id}"
    data = notified.data
    assert (data["buys"], data["sells"], data["rows"], data["published"]) == (2, 1, 3, "2026-10-07")
    assert [entry["ticker"] for entry in data["items"]] == ["NVDA", "AAPL", None]
    assert data["person"]["roles"] == ["house"] and data["url"].startswith("https://")


def test_a_linked_amended_report_is_marked():
    follow("oge:Donald J Trump|president")
    turn_on()
    with session() as s, s.begin():
        trump = politician(s, source="oge", name="Donald J Trump", district=None, position="president")
        old = report(s, trump, report_date=date(2026, 9, 1), status="superseded", discovered_at=START - timedelta(days=20),
                     rows=[("AAPL", "Apple", "purchase", date(2026, 8, 1), "1001", "15000")])
        report(s, trump, report_date=date(2026, 10, 6), amendment_status="linked", amendment_of=old.id,
               discovered_at=AT - timedelta(hours=1), rows=[("AAPL", "Apple", "purchase", date(2026, 8, 1), "15001", "50000")])
    assert run_scan() == 1
    [item] = notifications()
    assert item.data["amendment"] is True and item.data["person"]["roles"] == ["president"]


def index_member(cik="0000000123", ticker="ACME"):
    with session() as s, s.begin():
        s.add(IndexConstituent(index_name="sp500", ticker=ticker, cik=cik, name=ticker, updated_at=START))


def executive_buy(s, owner, *, shares="5000", price="25", issuer="0000000123", ticker="ACME", title="Chief Executive Officer",
                  plan=None, day=date(2026, 10, 8), accepted=AT - timedelta(hours=2)):
    return sec_trade(s, owner=owner, owner_name=f"Owner {owner}", issuer=issuer, ticker=ticker, code="P", day=day,
                     accepted=accepted, shares=shares, price=price, plan=plan,
                     roles={"is_officer": "1", "officer_title": title, "is_director": "0"})


def test_executive_purchase_threshold_and_scope():
    turn_on()
    index_member()
    with session() as s, s.begin():
        executive_buy(s, "0000000501")  # $125,000 by the CEO of an S&P 500 member
        executive_buy(s, "0000000502", shares="1000", title="Chief Financial Officer")  # $25,000: below
        executive_buy(s, "0000000503", issuer="0000000777", ticker="SMLL")  # not in an index or the watchlist
        executive_buy(s, "0000000504", plan=True, issuer="0000000124", ticker="ACMP")  # a plan trade
    index_member("0000000124", "ACMP")
    assert run_scan() == 1
    [item] = notifications("rule_executive")
    assert (item.issuer_id, item.data["amount"], item.data["person"]["roles"]) == ("0000000123", "125000.00", ["ceo"])
    assert run_scan() == 0
    # Every company, a lower threshold and plan trades included.
    turn_on(scope="all", executive_min=20000, exclude_plans=False)
    assert run_scan() == 3
    assert {item.issuer_id for item in notifications("rule_executive")} == {"0000000123", "0000000777", "0000000124"}


def test_the_watchlist_adds_companies_to_the_rules():
    turn_on()
    with session() as s, s.begin():
        executive_buy(s, "0000000503", issuer="0000000777", ticker="SMLL")
        s.add(WatchlistItem(ticker="SMLL", added_at=START))
    assert run_scan() == 1


def test_three_people_buying_one_company_within_seven_days():
    turn_on()
    index_member()
    with session() as s, s.begin():
        for number, day in enumerate((date(2026, 10, 2), date(2026, 10, 6)), 1):
            executive_buy(s, f"000000060{number}", shares="10", title="Director", day=day)
    assert run_scan() == 0
    with session() as s, s.begin():
        executive_buy(s, "0000000603", shares="10", title="Director", day=date(2026, 10, 8))
    assert run_scan() == 1
    [item] = notifications("rule_cluster")
    assert (item.data["people"], item.data["rows"], item.dedupe_key) == (3, 3, "rule_cluster:0000000123:2026-10-02")
    # A fourth buyer within the week does not notify again.
    with session() as s, s.begin():
        executive_buy(s, "0000000604", shares="10", title="Director", day=date(2026, 10, 9), accepted=AT - timedelta(minutes=5))
    assert run_scan() == 0


def test_rules_ignore_history_from_before_they_were_turned_on():
    index_member()
    with session() as s, s.begin():
        executive_buy(s, "0000000501", accepted=START - timedelta(hours=1), day=date(2026, 9, 30))
    turn_on()
    assert run_scan(START + timedelta(hours=1)) == 0


# --- phone pushes ---------------------------------------------------------------------------


class Transport:
    def __init__(self, answers=()):
        self.answers, self.sent = list(answers), []

    def __call__(self, server, message):
        self.sent.append((server, message))
        answer = self.answers.pop(0) if self.answers else (200, None)
        if isinstance(answer, Exception):
            raise answer
        return answer


def waiting(count, at=AT, **data):
    with session() as s, s.begin():
        for number in range(count):
            s.add(Notification(dedupe_key=f"trade:sec:test-{at.isoformat()}-{number}", kind="trade",
                               person_key="sec:0001767470", created_at=at + timedelta(seconds=number), push_state="pending",
                               data={"source": "sec", "person": {"key": "sec:0001767470", "name": "Ryan Cohen", "roles": ["ceo"]},
                                     "ticker": "GME", "asset_name": "GameStop Corp.", "buy": {"rows": 1, "amount": "25000.00"},
                                     "sell": None, "traded_from": "2026-10-06", "traded_to": "2026-10-06",
                                     "published": "2026-10-08", "url": "https://www.sec.gov/Archives/x-index.htm",
                                     "amendment": False, "rows": 1, **data}))


def leased():
    with session() as s, s.begin():
        push.request(s)
    job = claim({push.KIND})
    assert job is not None
    return job


def job_state(job_id):
    with session() as s:
        job = s.get(Job, job_id)
        return job.status, job.available_at, job.checkpoint


def test_singles_carry_public_facts_and_are_spaced(lifecycle_client):  # noqa: F811
    turn_on(ntfy=True)
    waiting(2)
    transport, pauses = Transport(), []
    push.run(leased(), lambda: False, transport=transport, sleep=pauses.append)
    assert len(transport.sent) == 2 and transport.sent[0][0] == "https://ntfy.sh"
    message = transport.sent[0][1]
    with session() as s:
        topic = settings.load(s)["ntfy"]["topic"]
    assert message["topic"] == topic and message["title"] == "Ryan Cohen: buy GME"
    assert "Traded 2026-10-06 · published 2026-10-08" in message["message"]
    assert message["click"] == "https://www.sec.gov/Archives/x-index.htm"
    # Nothing local: no host of this computer, no local path.
    assert "127.0.0.1" not in str(message) and "localhost" not in str(message) and "/api/" not in str(message)
    # The second waited for the minimum spacing.
    assert pauses and 0 < pauses[0] <= push.MIN_INTERVAL
    assert {item.push_state for item in notifications()} == {"sent"}
    status = lifecycle_client.get("/api/v1/notifications/settings").json()["push"]
    assert (status["last_status"], status["sent_today"], status["waiting"]) == ("sent", 2, 0)


def test_more_than_five_in_one_minute_go_as_one_digest():
    turn_on(ntfy=True)
    waiting(7)
    waiting(2, at=AT + timedelta(minutes=3))
    transport = Transport()
    push.run(leased(), lambda: False, transport=transport, sleep=lambda _: None)
    titles = [message["title"] for _, message in transport.sent]
    assert titles == ["IIRP: 7 new notifications", "Ryan Cohen: buy GME", "Ryan Cohen: buy GME"]
    assert transport.sent[0][1]["message"].count("\n") == 5  # five lines and "and 2 more"
    with session() as s:
        rows = s.scalars(select(NotificationPush).order_by(NotificationPush.created_at)).all()
    assert [(row.kind, row.count) for row in rows] == [("digest", 7), ("single", 1), ("single", 1)]


def test_failures_wait_longer_each_time_and_then_give_up():
    turn_on(ntfy=True)
    waiting(1)
    transport = Transport([(500, None)])
    job = leased()
    push.run(job, lambda: False, transport=transport, sleep=lambda _: None)
    status, available, checkpoint = job_state(job.id)
    assert status == "RETRY_WAIT" and checkpoint["failures"] == 1
    first_wait = available
    with session() as s, s.begin():
        s.execute(update(Job).where(Job.id == job.id).values(available_at=func.now()))
    job = claim({push.KIND})
    push.run(job, lambda: False, transport=Transport([httpx.ConnectError("refused")]), sleep=lambda _: None)
    status, available, checkpoint = job_state(job.id)
    assert status == "RETRY_WAIT" and checkpoint["failures"] == 2 and available - first_wait > timedelta(seconds=20)
    # After the sixth failure in a row the message is marked failed and the job moves on.
    with session() as s, s.begin():
        s.execute(update(Job).where(Job.id == job.id).values(available_at=func.now(), checkpoint={"failures": 5}))
    job = claim({push.KIND})
    push.run(job, lambda: False, transport=Transport([(503, None)]), sleep=lambda _: None)
    assert job_state(job.id)[0] == "SUCCEEDED"
    assert notifications()[0].push_state == "failed"
    with session() as s:
        failed = s.scalar(select(NotificationPush))
    assert (failed.status, failed.attempts) == ("failed", 6) and "notifications.push_status" in failed.error


def test_too_many_requests_waits_as_asked_and_the_daily_limit_holds():
    turn_on(ntfy=True)
    waiting(1)
    job = leased()
    push.run(job, lambda: False, transport=Transport([(429, 120)]), sleep=lambda _: None)
    status, available, checkpoint = job_state(job.id)
    assert status == "RETRY_WAIT" and checkpoint["failures"] == 0
    assert timedelta(seconds=100) < available - datetime.now(timezone.utc) <= timedelta(seconds=121)
    with session() as s, s.begin():
        stamp = datetime.now(timezone.utc) - timedelta(hours=1)
        s.add_all([NotificationPush(kind="single", status="sent", count=1, attempts=1, created_at=stamp, sent_at=stamp)
                   for _ in range(push.DAILY_LIMIT)])
        s.execute(update(Job).where(Job.id == job.id).values(available_at=func.now()))
    job = claim({push.KIND})
    transport = Transport()
    push.run(job, lambda: False, transport=transport, sleep=lambda _: None)
    assert transport.sent == [] and job_state(job.id)[0] == "RETRY_WAIT"


def test_a_test_push_goes_first_and_needs_pushes_on(lifecycle_client):  # noqa: F811
    refused = lifecycle_client.post("/api/v1/notifications/test")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "notifications.push_off"
    turn_on(ntfy=True)
    waiting(1)
    assert lifecycle_client.post("/api/v1/notifications/test").status_code == 202
    transport = Transport()
    push.run(claim({push.KIND}), lambda: False, transport=transport, sleep=lambda _: None)
    assert [message["title"] for _, message in transport.sent] == ["IIRP test notification", "Ryan Cohen: buy GME"]


def test_pushes_turned_off_keep_notifications_in_the_app_only():
    turn_on(ntfy=True)
    waiting(2)
    job = leased()
    with session() as s, s.begin():
        settings.update(s, {"ntfy": {"enabled": False}})
    transport = Transport()
    push.run(job, lambda: False, transport=transport, sleep=lambda _: None)
    assert transport.sent == [] and {item.push_state for item in notifications()} == {None}


def test_new_notifications_wait_for_the_phone_only_when_pushes_are_on():
    follow("sec:0001767470")
    turn_on(ntfy=True)
    with session() as s, s.begin():
        sec_trade(s, accepted=AT - timedelta(hours=1))
    run_scan()
    assert notifications()[0].push_state == "pending"
    push.tick()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == push.KIND)) == 1


def test_report_titles_count_purchases_and_sales():
    report_data = {"source": "house", "person": {"name": "Nancy Pelosi"}, "rows": 4, "buys": 3, "sells": 1, "items": [],
                   "published": "2026-10-07", "traded_from": "2026-09-18", "traded_to": "2026-09-20"}
    item = Notification(kind="trade", data=report_data)
    from iirp.notifications import texts

    assert texts.title(item, "en") == "Nancy Pelosi: 3 purchases, 1 sale"
    assert texts.title(item, "zh") == "Nancy Pelosi：买入 3 笔，卖出 1 笔"
    item.data = {**report_data, "sells": 0, "amendment": True}
    assert texts.title(item, "en") == "Nancy Pelosi: 3 purchases (amendment)"
    assert "Traded 2026-09-18 – 2026-09-20 · published 2026-10-07" in texts.body(item, "en")


def test_bell_list_unread_and_mark_read(lifecycle_client):  # noqa: F811
    turn_on()
    waiting(3)
    listed = lifecycle_client.get("/api/v1/notifications", params={"limit": 2}).json()
    assert (listed["unread"], listed["enabled"], listed["more"], len(listed["items"])) == (3, True, True, 2)
    newest = listed["items"][0]["id"]
    after = lifecycle_client.post("/api/v1/notifications/read", json={"ids": [newest]}).json()
    assert after["unread"] == 2 and after["items"][0]["read"] is True
    assert lifecycle_client.post("/api/v1/notifications/read", json={"all": True}).json()["unread"] == 0
