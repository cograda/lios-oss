"""The snag-register Sheets mirror writes with the CALLER's Google token
(2026-09-06 — the same rule PR #122 applied to Google Docs).

`snags/tools.py::_export_to_sheet` used to read a configured
`sheets_owner_account` and use *that* account's OAuth token whoever was
calling. Every path into it is a tool call, so there is always a bound
caller; it now uses the caller's own token, skips (with a logged reason,
never an exception — a snag write must not fail on a Sheets hiccup) when
the caller has none, and refuses to mirror a sheet someone else owns.
Unit tier: the session and the sheets capability are stubs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.auth.context import use_user
from app.integrations.snags import tools as snag_tools


class _Sheets:
    Export = object  # only used as a query target on the stub session

    def __init__(self):
        self.ensure_calls: list[dict] = []
        self.writes: list[dict] = []

    def ensure_export(self, session, **kw):
        self.ensure_calls.append(kw)
        return SimpleNamespace(spreadsheet_url="https://sheet", owner_account_email=kw["owner_account_email"])

    def write_rows(self, session, export, **kw):
        self.writes.append(kw)


@pytest.fixture
def wired(monkeypatch):
    sheets = _Sheets()
    monkeypatch.setattr("app.plugin.capabilities.get_capability", lambda name: sheets)
    monkeypatch.setattr(
        "app.plugin.config_store.plugin_config",
        lambda name: SimpleNamespace(sheets_share_with=["x@example.com"]),
    )
    return sheets


def _session(*, token=None, existing_export=None):
    """A session whose `query(Model)` answers OAuthToken / Export / Snag lookups."""
    from app.integrations.snags.models import Snag
    from app.models.tokens import OAuthToken

    session = MagicMock()

    def query(model):
        q = MagicMock()
        if model is OAuthToken:
            q.filter_by.return_value.first.return_value = token
        elif model is Snag:
            q.order_by.return_value.all.return_value = []
        else:  # sheets.Export
            q.filter_by.return_value.one_or_none.return_value = existing_export
        return q

    session.query.side_effect = query
    return session


def test_export_uses_the_callers_own_google_account(wired):
    token = SimpleNamespace(account_email="sam@example.com", user_id=2)
    with use_user(2):
        snag_tools._export_to_sheet(_session(token=token))

    assert wired.ensure_calls and wired.ensure_calls[0]["owner_account_email"] == "sam@example.com"
    assert wired.ensure_calls[0]["owner_user_id"] == 2
    assert wired.writes and wired.writes[0]["owner_user_id"] == 2


def test_caller_without_a_google_account_skips_without_raising(wired):
    with use_user(2):
        snag_tools._export_to_sheet(_session(token=None))
    assert wired.ensure_calls == [] and wired.writes == []


def test_a_sheet_owned_by_someone_else_is_not_mirrored_with_their_token(wired):
    """The `sheet_exports` row records who created the sheet. A different
    caller must not have it written with the owner's token on their behalf."""
    token = SimpleNamespace(account_email="sam@example.com", user_id=2)
    existing = SimpleNamespace(owner_account_email="alex@example.com")
    with use_user(2):
        snag_tools._export_to_sheet(_session(token=token, existing_export=existing))
    assert wired.ensure_calls == [] and wired.writes == []


def test_no_configured_owner_account_remains():
    from app.integrations.snags.manifest import MANIFEST

    assert "sheets_owner_account" not in MANIFEST.config_schema
