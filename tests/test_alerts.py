from datetime import UTC, datetime, timedelta

import pytest
from conftest import PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY

from climatectl import alerts, store
from climatectl.health import Finding

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
FURNACE = Finding("heat_no_rise:up", "Upstairs: furnace may not be working", "40 min...", "high")
COLD = Finding("below_floor:down", "Living Room is too cold", "54.5°F...", "urgent")


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "climatectl.db")


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.working = True

    def __call__(self, title: str, message: str, priority: str) -> bool:
        if self.working:
            self.sent.append((title, message, priority))
        return self.working


@pytest.fixture
def outbox():
    return Outbox()


def cycle(conn, outbox, minutes, *findings):
    return alerts.update(conn, list(findings), NOW + timedelta(minutes=minutes), outbox)


def test_new_problem_notifies_once(conn, outbox):
    assert cycle(conn, outbox, 0, FURNACE) == 1
    assert cycle(conn, outbox, 15, FURNACE) == 1
    assert outbox.sent == [(FURNACE.title, FURNACE.message, "high")]


def test_reminders(conn, outbox):
    for m in range(0, 12 * 60 + 1, 15):
        cycle(conn, outbox, m, FURNACE, COLD)
    furnace = [(t, p) for t, _, p in outbox.sent if "furnace" in t]
    cold = [(t, p) for t, _, p in outbox.sent if "cold" in t]
    assert furnace == [(FURNACE.title, "high"), (f"Still: {FURNACE.title}", "high")]  # 0 h, 12 h
    # Hourly, and only the first one at emergency priority: the phone repeats that one already.
    assert len(cold) == 13
    assert cold[:2] == [(COLD.title, "urgent"), (f"Still: {COLD.title}", "high")]


def test_clears_only_after_staying_away(conn, outbox):
    cycle(conn, outbox, 0, FURNACE)
    assert cycle(conn, outbox, 15) == 1  # one quiet cycle could be a flicker
    assert cycle(conn, outbox, 30, FURNACE) == 1  # it was: still the same alert
    assert cycle(conn, outbox, 45) == 1
    assert cycle(conn, outbox, 60) == 0
    assert outbox.sent == [
        (FURNACE.title, FURNACE.message, "high"),
        (f"Cleared: {FURNACE.title}", "Lasted about 30 min.", "low"),
    ]
    (row,) = conn.execute("SELECT opened_at, resolved_at FROM alert").fetchall()
    assert row == ("2026-10-30T12:00:00Z", "2026-10-30T13:00:00Z")


def test_a_problem_that_returns_is_a_new_alert(conn, outbox):
    cycle(conn, outbox, 0, FURNACE)
    cycle(conn, outbox, 30)
    cycle(conn, outbox, 120, FURNACE)
    assert [t for t, _, _ in outbox.sent] == [
        FURNACE.title,
        f"Cleared: {FURNACE.title}",
        FURNACE.title,
    ]
    assert conn.execute("SELECT count(*) FROM alert").fetchone() == (2,)


def test_failed_send_is_retried(conn, outbox):
    outbox.working = False
    cycle(conn, outbox, 0, FURNACE)
    outbox.working = True
    cycle(conn, outbox, 15, FURNACE)
    cycle(conn, outbox, 30, FURNACE)
    assert outbox.sent == [(FURNACE.title, FURNACE.message, "high")]


def test_without_a_sender_alerts_are_still_kept(conn, caplog):
    with caplog.at_level("INFO"):
        assert alerts.update(conn, [FURNACE], NOW, None) == 1
        assert alerts.update(conn, [], NOW + timedelta(hours=1), None) == 0
    assert f"ALERT {FURNACE.title}" in caplog.text and "cleared:" in caplog.text


def send(title, message, priority):
    return alerts.pushover(PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY)(title, message, priority)


def test_pushover_request(fakes, pushover_posts):
    assert send(FURNACE.title, FURNACE.message, "high")
    (post,) = pushover_posts
    assert post["url"] == alerts.PUSHOVER_URL
    assert post["data"] == {
        "token": PUSHOVER_APP_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "title": FURNACE.title,
        "message": FURNACE.message,
        "priority": 1,
    }


def test_urgent_is_an_emergency_that_repeats_until_acknowledged(fakes, pushover_posts):
    assert send(COLD.title, COLD.message, "urgent")
    (post,) = pushover_posts
    assert post["data"]["priority"] == 2
    assert (post["data"]["retry"], post["data"]["expire"]) == (300, 10800)


@pytest.mark.parametrize("priority, expected", [("default", 0), ("low", -1)])
def test_quieter_priorities(fakes, pushover_posts, priority, expected):
    send("t", "m", priority)
    assert pushover_posts[0]["data"]["priority"] == expected
    assert "retry" not in pushover_posts[0]["data"]


def test_pushover_failure_never_logs_the_secrets(monkeypatch, caplog):
    def down(url, data, **_):
        raise alerts.requests.ConnectionError(f"cannot reach {url} with {data}")

    monkeypatch.setattr(alerts.requests, "post", down)
    with caplog.at_level("WARNING"):
        assert not send("t", "m", "high")
    assert "alert not sent: ConnectionError" in caplog.text
    assert PUSHOVER_APP_TOKEN not in caplog.text and PUSHOVER_USER_KEY not in caplog.text


def test_pushover_rejection_logs_its_reason(monkeypatch, caplog):
    class Rejected:
        ok, status_code = False, 400

        def json(self):
            return {"status": 0, "errors": ["application token is invalid"]}

    monkeypatch.setattr(alerts.requests, "post", lambda *a, **k: Rejected())
    with caplog.at_level("WARNING"):
        assert not send("t", "m", "high")
    assert "alert not sent: HTTP 400 application token is invalid" in caplog.text
