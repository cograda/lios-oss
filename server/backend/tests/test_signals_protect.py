"""Protect payload parsing (unit tier, no DB) + device-key normalisation +
device-name resolution.

Normalised device-key form changed 2026-09-11 after a real "Test Alarm"
payload showed Protect sends MACs UPPERCASE WITHOUT COLONS
(`A89C6CB03B50`), while `HOME_SIGNALS_DEVICES` config is naturally written
colon-form (copied from HA's device registry). Lower-casing alone doesn't
make those two forms equal, so `normalize_device_key` also strips
separators — the stored/matched form is colon-free, e.g.
`"8c:ed:e1:72:f4:13"` -> `"8cede172f413"`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.integrations.signals.protect import normalize_device_key, parse_protect
from app.integrations.signals.routes import normalize_devices_config, resolve_device_name

REAL_SHAPE = {
    "alarm": {
        "name": "Comar Front Door",
        "sources": [{"device": "8C:ED:E1:72:F4:13", "type": "include"}],
        "conditions": [{"condition": {"type": "is", "source": "person"}}],
        "triggers": [
            {"key": "person", "device": "8C:ED:E1:72:F4:13",
             "eventId": "evt-123", "timestamp": 1757600000000},
        ],
        "eventPath": "/proxy/protect/api/events/evt-123",
        "eventLocalLink": "https://192.168.1.1/protect/evt-123",
    },
    "timestamp": 1757600000000,
}

# The real payload observed from a Protect Alarm Manager "Test Alarm": the
# trigger device is a placeholder, not the alarm's actual camera, but
# `sources[]` carries the real (colonless) MAC.
TEST_ALARM_SHAPE = {
    "alarm": {
        "name": "Milkman",
        "sources": [{"type": "include", "device": "A89C6CB03B50"}],
        "triggers": [{"key": "person", "device": "FAKE_MAC",
                      "eventId": "testEventId", "timestamp": 1789162652753}],
        "eventPath": "/protect/events/event/testEventId",
        "conditions": [{"condition": {"type": "is", "source": "person"}}],
        "eventLocalLink": "https://192.168.1.1/protect/events/event/testEventId",
    },
    "timestamp": 1789162652755,
}


def test_normalize_device_key_strips_colons_dashes_and_case():
    assert normalize_device_key("8C:ED:E1:72:F4:13") == "8cede172f413"
    assert normalize_device_key("8c-ed-e1-72-f4-13") == "8cede172f413"
    assert normalize_device_key(" A89C6CB03B50 ") == "a89c6cb03b50"
    assert normalize_device_key("") is None
    assert normalize_device_key(None) is None
    assert normalize_device_key(42) is None  # type: ignore[arg-type]


def test_normalize_device_key_config_and_payload_form_match():
    # This is the whole point: a config entry written colon-form must equal
    # what Protect actually sends (colonless, uppercase).
    assert normalize_device_key("a8:9c:6c:b0:3b:50") == normalize_device_key("A89C6CB03B50")


def test_parse_protect_known_shape():
    parsed = parse_protect(REAL_SHAPE)
    assert parsed.kind == "person"
    assert parsed.device_key == "8cede172f413"
    assert parsed.sources_device_keys == ["8cede172f413"]
    assert parsed.sender_event_id == "evt-123"
    assert parsed.occurred_at == datetime.fromtimestamp(1757600000, tz=timezone.utc)


def test_parse_protect_unknown_shape_never_raises():
    for body in ({}, {"foo": "bar"}, {"alarm": "not-a-dict"}, None, [], "garbage", 42):
        parsed = parse_protect(body)  # type: ignore[arg-type]
        assert parsed.kind == "unknown"
        assert parsed.device_key is None
        assert parsed.occurred_at is not None


def test_parse_protect_falls_back_to_alarm_sources_for_device():
    body = {
        "alarm": {
            "sources": [{"device": "AA:BB:CC:DD:EE:FF", "type": "include"}],
            "conditions": [{"condition": {"type": "is", "source": "vehicle"}}],
            "triggers": [{"key": "vehicle", "timestamp": 1757600000000}],  # no device on trigger
        },
    }
    parsed = parse_protect(body)
    assert parsed.kind == "vehicle"
    assert parsed.device_key == "aabbccddeeff"


def test_parse_protect_test_alarm_keeps_placeholder_trigger_device():
    """The real "Test Alarm" shape: trigger device is present (a placeholder,
    `FAKE_MAC`), so protect.py's own absent-device fallback does NOT kick in
    — `device_key` stays the raw trigger value. `sources_device_keys` still
    carries the real camera MAC for routes.py's config-aware fallback."""
    parsed = parse_protect(TEST_ALARM_SHAPE)
    assert parsed.device_key == "fake_mac"
    assert parsed.sources_device_keys == ["a89c6cb03b50"]
    assert parsed.sender_event_id == "testEventId"


def test_parse_protect_condition_source_fallback_when_no_trigger_key():
    body = {
        "alarm": {
            "triggers": [{"device": "AA:BB:CC:DD:EE:FF", "timestamp": 1757600000000}],
            "conditions": [{"condition": {"type": "is", "source": "package"}}],
        },
    }
    parsed = parse_protect(body)
    assert parsed.kind == "package"


def test_parse_protect_seconds_epoch_is_upgraded_to_ms():
    # Some firmwares have shipped seconds instead of ms; a plausible seconds
    # value is far below any ms value for a real date, so it's rescaled.
    body = {"alarm": {"triggers": [{"key": "motion", "timestamp": 1757600000}]}}
    parsed = parse_protect(body)
    assert parsed.occurred_at.year >= 2025


def test_resolve_device_name():
    devices = {"8cede172f413": "front_door"}
    assert resolve_device_name("8cede172f413", devices) == "front_door"
    assert resolve_device_name("aabbccddeeff", devices) is None
    assert resolve_device_name(None, devices) is None


def test_normalize_devices_config_normalises_keys_not_values():
    devices = normalize_devices_config({"8C:ED:E1:72:F4:13": "front_door", "AA-BB-CC-DD-EE-FF": "gate"})
    assert devices == {"8cede172f413": "front_door", "aabbccddeeff": "gate"}


def test_normalize_devices_config_handles_none_and_empty():
    assert normalize_devices_config(None) == {}
    assert normalize_devices_config({}) == {}
