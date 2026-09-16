"""Dashboard panel envelope helpers — V4 chunk 5.1.

`BaseIntegration.dashboard_data()` may optionally emit a self-describing
`{"panels": [...]}` envelope alongside its existing bespoke keys (additive,
not a replacement — the legacy top-level keys some integrations already
return, e.g. finance's `total_income`, are also read by `finance_summary`'s
MCP tool via the same `get_dashboard_stats()` helper, and stay exactly as
they were). The frontend's generic panel renderer (`components/dashboard/
panel-renderer.tsx`) consumes only `panels`; integrations that haven't
adopted the envelope yet render nothing there (harmless, not an error).

Three panel kinds cover every current dashboard use:
  - "stat"  — one number + optional trend, e.g. `{"value": 12, "trend": {...}}`
  - "list"  — a short list of label/value rows, e.g. reminders-by-list
  - "table" — a small table, e.g. this week's calendar events

These are plain dict builders (no DB access, no framework dependency) so
any integration can call them directly from its own `dashboard_data()`.
"""

from __future__ import annotations

from typing import Any


def stat_panel(
    title: str,
    value: Any,
    *,
    trend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"value": value}
    if trend is not None:
        data["trend"] = trend
    return {"kind": "stat", "title": title, "data": data}


def list_panel(title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """`items` — each `{"label": ..., "value": ..., "sublabel": ...?}`."""
    return {"kind": "list", "title": title, "data": {"items": items}}


def table_panel(title: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"kind": "table", "title": title, "data": {"columns": columns, "rows": rows}}
