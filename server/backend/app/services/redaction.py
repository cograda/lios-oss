"""Central redaction for the tool-call audit trail (V4 chunk 2.5).

`app.plugin.dispatch` calls `scrub_args()` on every tool call's arguments
before persisting them to `tool_calls.args_summary`. This is the one place
that decides what's safe to keep: drop anything that looks like a secret,
truncate long strings, and cap the whole serialized blob so one chatty call
(a giant CSV body, a huge notes field) can't bloat the audit table.

Deliberately conservative — false positives (redacting a harmless field
whose name happens to contain "token") are fine; false negatives (a real
secret slipping through) are not.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Case-insensitive match against the key name. Matches anywhere in the key
# so `csv_content`, `api_token`, `auth_secret` etc. all get caught.
_SENSITIVE_KEY_RE = re.compile(
    r"(token|password|secret|authorization|csv_content|body)", re.IGNORECASE,
)

_REDACTED = "[redacted]"
_MAX_STRING_LEN = 200
_MAX_BLOB_BYTES = 2048


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_STRING_LEN]
    if isinstance(value, dict):
        return _scrub_dict(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    return value


def _scrub_dict(d: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if _SENSITIVE_KEY_RE.search(str(k)):
            out[k] = _REDACTED
        else:
            out[k] = _scrub_value(v)
    return out


def scrub_args(arguments: dict | None) -> str:
    """Serialize tool-call arguments for the audit log.

    - Drops values for keys matching token|password|secret|authorization|
      csv_content|body (case-insensitive) — replaced with "[redacted]".
    - Truncates every remaining string value to 200 chars.
    - Caps the whole serialized blob at 2KB; if scrubbing+truncation still
      overflows that, the result is replaced with a small marker object
      carrying a hard-sliced partial, which itself is guaranteed <= 2KB.

    Never raises — arguments that don't serialize cleanly (non-JSON-able
    objects) fall back to `str()` via `default=str`.
    """
    if not arguments:
        return "{}"

    scrubbed = _scrub_dict(arguments)
    blob = json.dumps(scrubbed, default=str)
    if len(blob.encode("utf-8")) <= _MAX_BLOB_BYTES:
        return blob

    # Still too big after key-level truncation (e.g. hundreds of fields) —
    # hard-cap by shrinking a plain-text partial until the *re-serialized*
    # marker object fits. Always re-encoding via json.dumps (never slicing
    # already-escaped JSON text) keeps the result valid JSON at every step —
    # slicing post-escaping can land mid-escape-sequence and produce broken
    # JSON (e.g. cutting a `\"` in half).
    marker_overhead = len(json.dumps({"_truncated": True, "partial": ""}).encode("utf-8"))
    budget = max(_MAX_BLOB_BYTES - marker_overhead, 0)
    partial = blob[:budget]
    while True:
        capped = json.dumps({"_truncated": True, "partial": partial})
        if len(capped.encode("utf-8")) <= _MAX_BLOB_BYTES or not partial:
            break
        # Escaping can expand size (quotes, backslashes) — shrink and retry.
        partial = partial[: max(len(partial) - 64, 0)]
    return capped
