"""Pure parsing of Alertmanager's webhook payload (version "4") into rows
ready to insert as `AlertEvent`s. No DB, no I/O — kept separate from
`routes.py` so payload-shape parsing is unit-testable without a session.

Payload shape (Alertmanager's own docs):

    {
      "version": "4",
      "status": "firing" | "resolved",
      "groupLabels": {...}, "commonLabels": {...}, "commonAnnotations": {...},
      "alerts": [
        {
          "status": "firing" | "resolved",
          "labels": {"alertname": ..., "severity": ..., "page": ..., ...},
          "annotations": {"summary": ..., "description": ...},
          "startsAt": "2026-09-14T12:00:00Z",
          "endsAt": "0001-01-01T00:00:00Z",   # Go's zero value while firing
          "fingerprint": "abcd1234",
          ...
        },
        ...
      ]
    }

One row is built per `alerts[]` entry — a delivery's top-level `status`/
`groupLabels`/etc. are a summary of the group and are deliberately not what
gets stored; each alert already carries everything needed on its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Go's zero `time.Time`, serialised — what Alertmanager sends for `endsAt`
# on a still-firing alert. Never a real end time.
_GO_ZERO_TIME_PREFIX = "0001-01-01T00:00:00"


def _parse_ts(value: str | None) -> datetime | None:
    """ISO 8601 (Alertmanager always sends `Z`) -> aware `datetime`, or
    `None` for missing/unparseable/the Go zero-time sentinel."""
    if not value or value.startswith(_GO_ZERO_TIME_PREFIX):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_alerts(body: dict[str, Any]) -> list[dict[str, Any]]:
    """`body["alerts"]` -> a list of dicts shaped like `AlertEvent` columns.

    Tolerant of a malformed/missing `alerts` list (returns `[]`) and of a
    malformed individual alert entry (skipped, not raised) — a webhook
    receiver that 500s on one bad entry in a batch loses every other alert
    in the same delivery too.
    """
    alerts = body.get("alerts")
    if not isinstance(alerts, list):
        return []

    rows: list[dict[str, Any]] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        if not isinstance(labels, dict):
            labels = {}
        if not isinstance(annotations, dict):
            annotations = {}

        fingerprint = alert.get("fingerprint")
        alertname = labels.get("alertname")
        status = alert.get("status")
        starts_at = _parse_ts(alert.get("startsAt"))
        if not fingerprint or not alertname or status not in ("firing", "resolved") or starts_at is None:
            # Missing what the unique index / NOT NULL columns require —
            # skip rather than fail the whole delivery.
            continue

        rows.append({
            "fingerprint": str(fingerprint),
            "alertname": str(alertname),
            "status": status,
            "severity": labels.get("severity"),
            "page": labels.get("page"),
            "instance": labels.get("instance") or labels.get("host"),
            "summary": annotations.get("summary"),
            "description": annotations.get("description"),
            "starts_at": starts_at,
            "ends_at": _parse_ts(alert.get("endsAt")),
            "labels": labels,
            "annotations": annotations,
        })
    return rows
