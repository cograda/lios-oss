"""Audit the MCP tool layer for schema parameters that are declared but
never referenced in the handler body — the general shape of issue #172
(`ha_entities`'s `pattern`/`query` mismatch, and `ha_history`'s missing
`hours`) and of the pre-existing `gmail_search`-ignores-`since` bug it named
as the same class.

Usage: `python -m scripts.audit_declared_but_dropped_params` from
`server/backend/`.

Only checks handlers whose source lives inside the integration's own
package (`tools.py` or a sibling module such as `dupes.py`/`loops.py`) —
DSL-built tools (ListTool/SearchTool/etc.) route schema params through a
shared engine in `app/tools/*.py` rather than referencing the literal
string in the integration's own package, so those are skipped entirely
(a naive source-grep would false-positive on every one of them).

Heuristic, not proof: for each schema property name, checks whether the
handler function's own source text contains the literal `"<name>"` or
`'<name>'` anywhere. A handler that reads its arguments through a shared
private helper (e.g. `_scope_owner(session, args)`, `_apply_update(...)`)
will false-positive here, because the literal string lives in the helper,
not in the handler's own function body — inspect.getsource() only sees the
one function. Every flagged case must be read before being treated as a
real bug; this script narrows where to look, it does not replace reading
the code.

As of 2026-09-08 (issue #172), every finding from a run of this script
traced back to exactly that indirection pattern — a shared resolver
function genuinely honouring the parameter — with no further real drops
found beyond the two already fixed in `homeassistant/tools.py`. Re-run
after adding a new hand-written CustomTool handler.
"""
import importlib
import inspect
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

# Importing app.integrations.*.tools pulls in app.db, which needs these set
# but never actually connects for this script (no session is opened).
os.environ.setdefault("HOME_DATABASE__URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("HOME_OAUTH_ENCRYPTION_KEY", "x" * 32)


def main() -> int:
    integrations_dir = os.path.join(BACKEND, "app", "integrations")
    names = sorted(
        d for d in os.listdir(integrations_dir)
        if os.path.isdir(os.path.join(integrations_dir, d))
        and not d.startswith("_")
        and os.path.exists(os.path.join(integrations_dir, d, "tools.py"))
    )

    findings: list[tuple[str, str, list[str]]] = []
    skipped: list[tuple[str, str]] = []

    for name in names:
        try:
            mod = importlib.import_module(f"app.integrations.{name}.tools")
        except Exception as e:  # noqa: BLE001 - audit script, report and continue
            skipped.append((name, f"import failed: {e}"))
            continue

        getter = getattr(mod, "get_mcp_tools", None) or getattr(mod, "mcp_tools", None)
        if getter is None:
            skipped.append((name, "no get_mcp_tools()/mcp_tools() found"))
            continue

        try:
            tools = getter()
        except Exception as e:  # noqa: BLE001
            skipped.append((name, f"{getter.__name__}() failed: {e}"))
            continue

        for tool in tools:
            tool_name = tool.get("name", "?")
            handler = tool.get("handler")
            schema = tool.get("input_schema") or tool.get("inputSchema") or {}
            props = list((schema.get("properties") or {}).keys())
            if not props or handler is None:
                continue

            try:
                hmod = inspect.getmodule(handler)
            except Exception:  # noqa: BLE001
                hmod = None
            if hmod is None or not hmod.__name__.startswith(f"app.integrations.{name}."):
                continue  # DSL-generated closure, not hand-written here

            try:
                src = inspect.getsource(handler)
            except (OSError, TypeError):
                continue

            missing = [p for p in props if f'"{p}"' not in src and f"'{p}'" not in src]
            if missing:
                findings.append((name, tool_name, missing))

    print("=== Skipped modules ===")
    for name, reason in skipped:
        print(f"  {name}: {reason}")

    print("\n=== Declared-but-possibly-dropped schema params (heuristic — verify each) ===")
    if not findings:
        print("(none found)")
    for integ, tool_name, missing in findings:
        print(f"  {integ}.{tool_name}: {missing}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
