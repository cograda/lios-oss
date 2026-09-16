"""Unit tests for app.services.redaction.scrub_args (V4 chunk 2.5).

Pure function, no DB — the dispatcher-integration is covered separately in
test_tool_call_runs.py.
"""

import json

from app.services.redaction import scrub_args


def test_empty_or_none_returns_empty_object():
    assert scrub_args(None) == "{}"
    assert scrub_args({}) == "{}"


def test_secret_keys_are_redacted():
    args = {
        "token": "super-secret-value",
        "password": "hunter2",
        "api_secret": "abc123",
        "Authorization": "Bearer xyz",
        "csv_content": "a,b,c\n1,2,3",
        "body": "raw request body",
        "summary": "totally fine value",
    }
    parsed = json.loads(scrub_args(args))
    assert parsed["token"] == "[redacted]"
    assert parsed["password"] == "[redacted]"
    assert parsed["api_secret"] == "[redacted]"
    assert parsed["Authorization"] == "[redacted]"
    assert parsed["csv_content"] == "[redacted]"
    assert parsed["body"] == "[redacted]"
    assert parsed["summary"] == "totally fine value"


def test_nested_secret_keys_are_redacted():
    args = {"outer": {"inner_token": "leak-me", "fine": "ok"}}
    parsed = json.loads(scrub_args(args))
    assert parsed["outer"]["inner_token"] == "[redacted]"
    assert parsed["outer"]["fine"] == "ok"


def test_long_strings_are_truncated_to_200_chars():
    long_value = "x" * 5000
    parsed = json.loads(scrub_args({"notes": long_value}))
    assert len(parsed["notes"]) == 200


def test_long_strings_in_lists_are_truncated():
    long_value = "y" * 1000
    parsed = json.loads(scrub_args({"items": [long_value, long_value]}))
    assert all(len(v) == 200 for v in parsed["items"])


def test_whole_blob_capped_at_2kb():
    # Many distinct short-ish keys so key-level truncation alone doesn't
    # bring it under budget — forces the hard-cap path.
    args = {f"field_{i}": "z" * 150 for i in range(50)}
    blob = scrub_args(args)
    assert len(blob.encode("utf-8")) <= 2048


def test_capped_blob_is_still_valid_json():
    args = {f"field_{i}": "z" * 150 for i in range(50)}
    blob = scrub_args(args)
    parsed = json.loads(blob)  # must not raise
    assert isinstance(parsed, dict)


def test_non_string_values_pass_through():
    parsed = json.loads(scrub_args({"count": 3, "ok": True, "ratio": 1.5, "none": None}))
    assert parsed == {"count": 3, "ok": True, "ratio": 1.5, "none": None}
