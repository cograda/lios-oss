"""Read side: `alert_events_since` — what fired/cleared since a timestamp.

Built for the kickoff/check-in loop this issue exists for: instead of every
alert buzzing a phone the instant it fires, a person reviews what happened
since the last note, as a plain "these fired/cleared" list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.alerts.models import AlertEvent


def _serialise(row: AlertEvent) -> dict[str, Any]:
    return {
        "fingerprint": row.fingerprint,
        "alertname": row.alertname,
        "severity": row.severity,
        "page": row.page,
        "instance": row.instance,
        "summary": row.summary,
        "description": row.description,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "received_at": row.received_at.isoformat() if row.received_at else None,
    }


def alert_events_since(session: Session, since: datetime, limit: int = 200) -> dict[str, Any]:
    """Everything that fired or cleared since `since`.

    - `fired`: firing events received since `since`, deduped by fingerprint
      (newest delivery per fingerprint wins), newest first, each carrying
      `still_firing` — whether the SAME fingerprint has a later `resolved`
      row than this firing's `starts_at`. A fingerprint that fired and
      resolved within the window shows up once, in `cleared`, not twice.
    - `cleared`: resolved events received since `since`, newest first.
    - Counts split by `page == "phone"` (the ones that were meant to buzz a
      handset — see `models.py`'s docstring) vs everything else (FYI).
    """
    rows = (
        session.query(AlertEvent)
        .filter(AlertEvent.received_at >= since)
        .order_by(AlertEvent.received_at.desc())
        .limit(max(1, limit))
        .all()
    )

    # Latest row per (fingerprint, status) — a repeat delivery inside the
    # window (Alertmanager's own repeat_interval) must not produce repeat
    # entries in either list.
    latest_firing: dict[str, AlertEvent] = {}
    latest_resolved: dict[str, AlertEvent] = {}
    for row in rows:
        bucket = latest_resolved if row.status == "resolved" else latest_firing
        existing = bucket.get(row.fingerprint)
        if existing is None or row.received_at > existing.received_at:
            bucket[row.fingerprint] = row

    # A fingerprint that both fired and resolved in the window is reported
    # as cleared, not as still-firing — the resolution is the more current
    # fact about it.
    firing_only = {
        fp: row for fp, row in latest_firing.items() if fp not in latest_resolved
    }

    # `still_firing` for a `fired` entry: true unless a later resolution for
    # the same fingerprint exists ANYWHERE in the table (not just in this
    # window) — a fingerprint that fired before `since` and is still open
    # never shows up as resolved in-window, and reporting it as unresolved
    # is exactly right.
    fingerprints = list(firing_only.keys())
    still_open: set[str] = set(fingerprints)
    if fingerprints:
        resolved_after = (
            session.query(AlertEvent.fingerprint, AlertEvent.starts_at)
            .filter(
                AlertEvent.status == "resolved",
                AlertEvent.fingerprint.in_(fingerprints),
            )
            .all()
        )
        resolved_by_fp: dict[str, Any] = {}
        for fp, starts_at in resolved_after:
            existing = resolved_by_fp.get(fp)
            if existing is None or starts_at > existing:
                resolved_by_fp[fp] = starts_at
        for fp, row in firing_only.items():
            resolved_starts_at = resolved_by_fp.get(fp)
            if resolved_starts_at is not None and resolved_starts_at >= row.starts_at:
                still_open.discard(fp)

    fired = [
        {**_serialise(row), "still_firing": fp in still_open}
        for fp, row in sorted(
            firing_only.items(), key=lambda kv: kv[1].received_at, reverse=True
        )
    ]
    cleared = [
        _serialise(row)
        for row in sorted(
            latest_resolved.values(), key=lambda r: r.received_at, reverse=True
        )
    ]

    def _is_phone(entries: list[dict]) -> int:
        return sum(1 for e in entries if e.get("page") == "phone")

    return {
        "since": since.isoformat(),
        "fired": fired,
        "cleared": cleared,
        "counts": {
            "fired_total": len(fired),
            "fired_phone": _is_phone(fired),
            "fired_fyi": len(fired) - _is_phone(fired),
            "cleared_total": len(cleared),
            "cleared_phone": _is_phone(cleared),
            "cleared_fyi": len(cleared) - _is_phone(cleared),
        },
    }


def recent_event_count(session: Session) -> int:
    """Cheap presence check — used by `dashboard_data()`."""
    return session.query(func.count(AlertEvent.id)).scalar() or 0
