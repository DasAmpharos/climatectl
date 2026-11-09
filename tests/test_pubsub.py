import google.auth
import pytest
from fake_pubsub import SUBSCRIPTION, FakePubSub, hvac
from google.auth.exceptions import DefaultCredentialsError

from climatectl.envfile import ConfigError
from climatectl.providers.nest import pubsub


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ("GCP_PROJECT_ID=climatectl-123\n", SUBSCRIPTION),
        (
            "GCP_PROJECT_ID=climatectl-123\nPUBSUB_SUBSCRIPTION=other-sub\n",
            "projects/climatectl-123/subscriptions/other-sub",
        ),
        (f"PUBSUB_SUBSCRIPTION={SUBSCRIPTION}\n", SUBSCRIPTION),
    ],
)
def test_subscription_path(env_file, lines, expected):
    env_file.write_text(lines)
    assert pubsub.subscription_path() == expected


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ("", "GCP_PROJECT_ID"),
        ("PUBSUB_SUBSCRIPTION=projects/x/topics/sdm-events\n", "Not a Pub/Sub subscription"),
    ],
)
def test_subscription_path_errors(env_file, lines, message):
    env_file.write_text(lines)
    with pytest.raises(ConfigError, match=message):
        pubsub.subscription_path()


def test_parse_resource_update():
    fake = FakePubSub()
    fake.publish(hvac("evt-1", "2026-10-24T01:02:03.456789Z", "down", "HEATING"))
    event = pubsub.parse(next(iter(fake.waiting.values())))
    assert event.ack_id == "ack-1"
    assert event.event_id == "evt-1"
    assert event.at == "2026-10-24T01:02:03Z"
    assert event.device_id == "down"
    assert event.kind == "resourceUpdate"
    assert event.traits == {"sdm.devices.traits.ThermostatHvac": {"status": "HEATING"}}


def test_parse_relation_update():
    fake = FakePubSub()
    fake.publish(
        {
            "eventId": "evt-2",
            "timestamp": "2026-10-24T01:05:00Z",
            "relationUpdate": {
                "type": "UPDATED",
                "subject": "enterprises/p/structures/s/rooms/r",
                "object": "enterprises/p/devices/up",
            },
        }
    )
    event = pubsub.parse(next(iter(fake.waiting.values())))
    assert (event.kind, event.device_id, event.traits) == ("relationUpdate", "up", {})


def test_parse_unreadable_data_is_kept():
    fake = FakePubSub()
    fake.publish(b"\xff not json")
    event = pubsub.parse(next(iter(fake.waiting.values())))
    assert (event.kind, event.event_id, event.at) == ("unknown", "msg-1", "2026-10-24T01:00:00Z")


def test_pull_and_acknowledge():
    fake = FakePubSub()
    fake.publish(hvac("evt-1", "2026-10-24T01:00:00Z", "down", "HEATING"))
    sub = pubsub.Subscriber(SUBSCRIPTION, session=fake)
    events = sub.pull()
    assert [e.event_id for e in events] == ["evt-1"]
    assert [e.event_id for e in sub.pull()] == ["evt-1"]  # not acked yet, so redelivered
    sub.acknowledge([e.ack_id for e in events])
    assert sub.pull() == []


def test_pull_error():
    fake = FakePubSub()
    fake.fail = True
    with pytest.raises(pubsub.PubSubError, match="403: PERMISSION_DENIED"):
        pubsub.Subscriber(SUBSCRIPTION, session=fake).pull()


def test_missing_credentials_explains_both_setups(monkeypatch):
    def no_credentials(**_):
        raise DefaultCredentialsError("none")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    with pytest.raises(pubsub.PubSubError, match="application-default login") as excinfo:
        pubsub.Subscriber(SUBSCRIPTION).pull()
    assert "GOOGLE_APPLICATION_CREDENTIALS" in str(excinfo.value)
