"""`vision.client._coerce_compare_json` — defensive parsing of the model's
reply for `vision.watch`'s compare() (unit tier, no network/DB)."""

from __future__ import annotations

from app.integrations.vision.client import _coerce_compare_json


def test_coerce_compare_json_clean():
    raw = '{"answer": true, "confidence": 0.92, "where": "bottom of frame", "notes": "a white box"}'
    out = _coerce_compare_json(raw)
    assert out == {"answer": True, "confidence": 0.92, "where": "bottom of frame", "notes": "a white box"}


def test_coerce_compare_json_fenced():
    raw = "Sure, here you go:\n```json\n{\"answer\": false, \"confidence\": 0.1, \"where\": null, \"notes\": \"nothing there\"}\n```"
    out = _coerce_compare_json(raw)
    assert out["answer"] is False
    assert out["confidence"] == 0.1
    assert out["where"] is None


def test_coerce_compare_json_confidence_clamped():
    raw = '{"answer": true, "confidence": 5, "where": "x", "notes": "y"}'
    out = _coerce_compare_json(raw)
    assert out["confidence"] == 1.0

    raw2 = '{"answer": true, "confidence": -3, "where": "x", "notes": "y"}'
    assert _coerce_compare_json(raw2)["confidence"] == 0.0


def test_coerce_compare_json_string_answer():
    raw = '{"answer": "yes", "confidence": 0.5, "where": null, "notes": ""}'
    assert _coerce_compare_json(raw)["answer"] is True

    raw2 = '{"answer": "no", "confidence": 0.5, "where": null, "notes": ""}'
    assert _coerce_compare_json(raw2)["answer"] is False


def test_coerce_compare_json_unparseable_falls_back_to_no():
    out = _coerce_compare_json("I cannot determine this from the images provided.")
    assert out["answer"] is False
    assert out["confidence"] == 0.0
    assert out["where"] is None
    assert "cannot determine" in out["notes"]


def test_coerce_compare_json_empty_string():
    out = _coerce_compare_json("")
    assert out["answer"] is False
    assert out["confidence"] == 0.0


def test_coerce_compare_json_missing_notes_falls_back_to_raw():
    raw = '{"answer": true, "confidence": 0.8}'
    out = _coerce_compare_json(raw)
    assert out["answer"] is True
    # notes missing from the JSON but the JSON itself parsed, so notes is ""
    assert out["notes"] == ""
