"""Turns health findings into notifications: one when a problem starts, reminders while it
lasts, and one when it clears. Notifications go to Pushover (https://pushover.net) when
PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY are set; either way every alert is kept in the `alert`
table and logged.

A problem must be gone for CLEAR_AFTER before it counts as cleared, so a check that flickers
between cycles stays one alert instead of a stream of them. A notification that fails to send
is retried next cycle."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

import requests

from .health import Finding
from .store import parse_utc

log = logging.getLogger("climatectl")

CLEAR_AFTER = timedelta(minutes=30)
REMIND_EVERY = {"urgent": timedelta(hours=1)}
DEFAULT_REMIND = timedelta(hours=12)
# An urgent alert already repeats on the phone until someone acknowledges it, so its hourly
# reminders go out one step lower instead of restarting the siren.
REMIND_PRIORITY = {"urgent": "high"}

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
# Emergency (2) repeats every RETRY until acknowledged in the app, for at most EXPIRE, and can
# sound through silent mode and Do Not Disturb when Critical Alerts are allowed on the phone.
# High (1) skips Pushover's quiet hours; low (-1) arrives without a sound.
PUSHOVER_PRIORITY = {"urgent": 2, "high": 1, "default": 0, "low": -1}
EMERGENCY_RETRY = timedelta(minutes=5)
EMERGENCY_EXPIRE = timedelta(hours=3)

Sender = Callable[[str, str, str], bool]  # (title, message, priority) -> sent?


def pushover(app_token: str, user_key: str) -> Sender:
    """A sender that posts to Pushover. `user_key` can be a delivery group's key, so each
    person acknowledges on their own phone. Never log either value."""

    def send(title: str, message: str, priority: str) -> bool:
        data = {
            "token": app_token,
            "user": user_key,
            "title": title,
            "message": message,
            "priority": PUSHOVER_PRIORITY[priority],
        }
        if priority == "urgent":
            data["retry"] = int(EMERGENCY_RETRY.total_seconds())
            data["expire"] = int(EMERGENCY_EXPIRE.total_seconds())
        try:
            resp = requests.post(PUSHOVER_URL, data=data, timeout=10)
        except requests.RequestException as e:
            log.warning("alert not sent: %s", type(e).__name__)
            return False
        if not resp.ok:
            # Pushover's error list names the problem (e.g. "application token is invalid")
            # without echoing the token or key back.
            try:
                errors = "; ".join(resp.json().get("errors", []))
            except ValueError:
                errors = ""
            log.warning("alert not sent: HTTP %s %s", resp.status_code, errors)
        return resp.ok

    return send


def update(
    conn: sqlite3.Connection, findings: list[Finding], now: datetime, send: Sender | None
) -> int:
    """Record this cycle's findings, notify as due, and return how many alerts are open."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    open_alerts = {
        row[1]: row
        for row in conn.execute(
            "SELECT id, key, title, priority, opened_at, last_seen_at, notified_at FROM alert"
            " WHERE resolved_at IS NULL"
        )
    }
    for f in findings:
        row = open_alerts.pop(f.key, None)
        if row is None:
            log.warning("ALERT %s: %s", f.title, f.message)
            alert_id = conn.execute(
                "INSERT INTO alert (key, title, message, priority, opened_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (f.key, f.title, f.message, f.priority, stamp, stamp),
            ).lastrowid
            title, priority, due = f.title, f.priority, True
        else:
            alert_id, notified_at = row[0], row[6]
            conn.execute(
                "UPDATE alert SET title = ?, message = ?, priority = ?, last_seen_at = ?"
                " WHERE id = ?",
                (f.title, f.message, f.priority, stamp, alert_id),
            )
            remind = REMIND_EVERY.get(f.priority, DEFAULT_REMIND)
            due = notified_at is None or now - parse_utc(notified_at) >= remind
            title, priority = f.title, f.priority
            if notified_at is not None:
                title, priority = f"Still: {f.title}", REMIND_PRIORITY.get(f.priority, f.priority)
        if due and send is not None and send(title, f.message, priority):
            conn.execute("UPDATE alert SET notified_at = ? WHERE id = ?", (stamp, alert_id))

    for (
        alert_id,
        _key,
        title,
        _priority,
        opened_at,
        last_seen_at,
        notified_at,
    ) in open_alerts.values():
        if now - parse_utc(last_seen_at) < CLEAR_AFTER:
            continue
        conn.execute("UPDATE alert SET resolved_at = ? WHERE id = ?", (stamp, alert_id))
        lasted = parse_utc(last_seen_at) - parse_utc(opened_at)
        log.info("cleared: %s", title)
        if send is not None and notified_at is not None:
            send(f"Cleared: {title}", f"Lasted about {_span(lasted)}.", "low")
    conn.commit()
    (count,) = conn.execute("SELECT count(*) FROM alert WHERE resolved_at IS NULL").fetchone()
    return count


def _span(span: timedelta) -> str:
    minutes = max(1, round(span.total_seconds() / 60))
    return f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"
