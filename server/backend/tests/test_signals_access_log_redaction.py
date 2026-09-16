"""`uvicorn.access` must never print the signals inlet's `?key=...` shared
secret verbatim (unit tier — a fake `LogRecord`, no real server involved).

Uvicorn's default `AccessFormatter` passes the request line (and other
fields) as `record.args`, substituted into the format string at emit time —
NOT already baked into `record.msg`. So the filter has to rewrite
`record.args`, not just `record.msg`; a filter that only checked `record.msg`
would look like it worked (the raw attribute is unchanged either way you
test it wrong) and then leak the key the moment a real access line goes
through uvicorn's own formatter.
"""

from __future__ import annotations

import logging

from app.integrations.signals.routes import _RedactSignalKeyFilter, install_log_filters


def _make_record(args) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s" %s', args=args, exc_info=None,
    )


def test_filter_redacts_key_in_request_line_args():
    record = _make_record(
        ("127.0.0.1:12345", 'POST /api/v1/signals/protect?key=s3cr3t HTTP/1.1', 200)
    )
    _RedactSignalKeyFilter().filter(record)
    assert "s3cr3t" not in record.args[1]
    assert "key=…" in record.args[1]
    # Unrelated args are untouched.
    assert record.args[0] == "127.0.0.1:12345"
    assert record.args[2] == 200


def test_filter_redacts_key_stops_at_ampersand():
    record = _make_record(
        ("127.0.0.1:1", 'POST /api/v1/signals/protect?key=s3cr3t&other=1 HTTP/1.1', 200)
    )
    _RedactSignalKeyFilter().filter(record)
    assert "s3cr3t" not in record.args[1]
    assert "other=1" in record.args[1]


def test_filter_leaves_records_without_a_key_alone():
    record = _make_record(("127.0.0.1:1", "GET /api/v1/health HTTP/1.1", 200))
    original = record.args
    _RedactSignalKeyFilter().filter(record)
    assert record.args == original


def test_filter_handles_msg_only_records_too():
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='request: /api/v1/signals/protect?key=s3cr3t', args=(), exc_info=None,
    )
    _RedactSignalKeyFilter().filter(record)
    assert "s3cr3t" not in record.msg
    assert "key=…" in record.msg


def test_filter_returns_true_always():
    record = _make_record(("a", "b", 200))
    assert _RedactSignalKeyFilter().filter(record) is True


def test_install_log_filters_is_idempotent():
    access_logger = logging.getLogger("uvicorn.access")
    before = len(access_logger.filters)
    install_log_filters()
    install_log_filters()
    install_log_filters()
    after = len(access_logger.filters)
    assert after == before + 1
