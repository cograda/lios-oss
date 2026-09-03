"""sam-rollout Phase B2 — per-user curated command set.

Covers:
  1. Template rendering per user (`app.prompts.commands`) — paths differ
     correctly between alex/sam, no cross-user literals leak through.
  2. `GET /api/v1/commands` auth (401 without bearer) and per-user shape.
  3. Drift: rendering alex's set must match the committed golden snapshots
     under `tests/snapshots/commands/*.md` byte-for-byte — an un-ported
     template change fails this test with a pointer back to the templates.

     Anchoring on snapshots rather than on `.claude/commands/*.md` is a
     consequence of the dev/day-to-day project split (2026-07-28): the
     delivered files now live in `vault/.claude/commands/`, which is
     gitignored and absent on a fresh clone, so there is nothing committed
     to compare against. The snapshot preserves the property that actually
     mattered — a template edit shows up as a reviewable diff in CI.

Unit tier throughout (no DB) — the endpoint test monkeypatches
`get_current_user` the same way `test_sam_rollout_a2_a3.py` does for the
apple_health/apple_reminders push routes.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.prompts.commands import (
    CURATED_COMMANDS,
    _apply_conditionals,
    context_for_user,
    render_claude_md,
    render_command,
    render_command_set,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# Golden snapshots of Alex's rendered set. The delivered copies live in the
# gitignored `vault/.claude/commands/` (see scripts/render_commands.py), so
# these committed snapshots are what CI can actually compare against.
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots" / "commands"

# Hand-authored, non-generated day-to-day commands — versioned source.
STATIC_COMMANDS_DIR = REPO_ROOT / "commands"

# Kept in sync with scripts/render_commands.py::STATIC_NON_COMMANDS.
STATIC_NON_COMMANDS = {"README.md"}


def _static_command_paths() -> list[Path]:
    return [
        p for p in sorted(STATIC_COMMANDS_DIR.glob("*.md"))
        if p.name not in STATIC_NON_COMMANDS
    ]


# ---------------------------------------------------------------------------
# 1. Template rendering per user
# ---------------------------------------------------------------------------

class TestPerUserRendering:
    def test_curated_set_has_six_commands(self):
        assert set(CURATED_COMMANDS) == {
            "daily-note", "add-task", "find", "triage", "lock-in", "week-ahead",
        }

    def test_alex_and_sam_daily_notes_paths_differ(self):
        alex = render_command_set("alex")
        sam = render_command_set("sam")

        assert "vault/Daily Notes/Alex/YYYY-MM-DD.md" in alex["daily-note.md"]
        assert "vault/Daily Notes/YYYY-MM-DD.md" in sam["daily-note.md"]
        # Sam's render must not carry Alex's per-user subfolder literal.
        assert "Daily Notes/Alex/" not in sam["daily-note.md"]

    def test_sam_has_no_vault_subfolder_in_triage_state_path(self):
        sam = render_command_set("sam")
        alex = render_command_set("alex")

        assert "vault/.triage-state.md" in sam["triage.md"]
        assert "vault/Alex/.triage-state.md" in alex["triage.md"]
        assert "vault/Alex/.triage-state.md" not in sam["triage.md"]

    def test_display_name_substituted(self):
        alex = render_command_set("alex")
        sam = render_command_set("sam")

        assert "Create or open today's daily note for Alex." in alex["daily-note.md"]
        assert "Create or open today's daily note for Sam." in sam["daily-note.md"]

    def test_lock_in_drops_the_alex_specific_sam_clause(self):
        sam = render_command_set("sam")
        assert "(or Sam's if specified in arguments)" not in sam["lock-in.md"]

    def test_generated_header_present(self):
        for name, content in render_command_set("sam").items():
            assert content.startswith("<!-- GENERATED"), name
            assert "do not hand-edit" in content

    def test_unknown_user_falls_back_to_sam_shape(self):
        ctx = context_for_user("someone-new", "Someone")
        assert ctx.daily_notes_dir == "Daily Notes/"
        assert ctx.display_name == "Someone"

    def test_claude_md_differs_and_has_no_alex_personal_content(self):
        alex_md = render_claude_md("alex")
        sam_md = render_claude_md("sam")
        assert alex_md != sam_md
        assert "is authoritative for the" in alex_md
        # Sam's vault-rules render must not carry Alex's personal folders.
        for leak in ("Health/", "MRI", "knee"):
            assert leak not in sam_md


# ---------------------------------------------------------------------------
# 2. Endpoint auth + shape
# ---------------------------------------------------------------------------

def _fake_db_instance(session):
    class _FakeDb:
        def session(self):
            @contextlib.contextmanager
            def _cm():
                yield session
            return _cm()
    return _FakeDb()


class TestPreferenceConditionals:
    """The `{% if %}` block machinery in `_apply_conditionals`.

    Worth testing directly rather than only through the rendered commands: a
    bug here doesn't raise, it silently ships a block the user asked not to
    see (or drops one they need), which is invisible until someone reads their
    morning note closely.
    """

    PREFS = {
        "daily_note.sections": ["pulse", "today"],
        "health.track_sleep": False,
        "health.strength_target": 0,
        "health.profile_path": "",
        "daily_note.show_listening": True,
    }

    def _render(self, body, prefs=PREFS):
        return _apply_conditionals(body, prefs, "test")

    def test_keeps_enabled_section(self):
        out = self._render("{% if section:pulse %}\nkeep\n{% endif %}")
        assert "keep" in out

    def test_drops_disabled_section(self):
        out = self._render("{% if section:coffee %}\ndrop\n{% endif %}")
        assert "drop" not in out

    def test_falsey_pref_drops_block(self):
        assert "drop" not in self._render(
            "{% if pref:health.strength_target %}\ndrop\n{% endif %}"
        )

    def test_negation(self):
        out = self._render("{% if not pref:health.track_sleep %}\nkeep\n{% endif %}")
        assert "keep" in out

    def test_nesting_binds_to_the_right_endif(self):
        """The bug a non-greedy regex would have introduced.

        The outer block is live and the inner one is not, so only the inner
        content drops — `after` must survive. A regex would have closed the
        outer block on the inner `{% endif %}` and leaked a stray marker.
        """
        out = self._render(
            "{% if section:pulse %}\n"
            "before\n"
            "{% if pref:health.track_sleep %}\n"
            "inner\n"
            "{% endif %}\n"
            "after\n"
            "{% endif %}"
        )
        assert "before" in out and "after" in out
        assert "inner" not in out
        assert "{%" not in out

    def test_disabled_outer_drops_enabled_inner(self):
        out = self._render(
            "{% if section:coffee %}\n"
            "{% if pref:daily_note.show_listening %}\n"
            "inner\n"
            "{% endif %}\n"
            "{% endif %}"
        )
        assert "inner" not in out and "{%" not in out

    def test_none_prefs_renders_everything(self):
        """No prefs means render all — keeps snapshot goldens stable and makes
        a block removable only by an explicit preference."""
        body = "{% if section:coffee %}\nkeep\n{% endif %}"
        assert "keep" in _apply_conditionals(body, None, "test")

    def test_unbalanced_endif_raises(self):
        with pytest.raises(ValueError, match="no open"):
            self._render("text\n{% endif %}")

    def test_unclosed_if_raises(self):
        with pytest.raises(ValueError, match="unclosed"):
            self._render("{% if section:pulse %}\ntext")

    def test_inline_marker_raises(self):
        with pytest.raises(ValueError, match="alone on its own line"):
            self._render("text {% if section:pulse %} more\n{% endif %}")

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unsupported condition"):
            self._render("{% if wat:nope %}\nx\n{% endif %}")

    def test_unknown_condition_inside_dropped_block_is_not_evaluated(self):
        """A dropped branch is never evaluated, so a condition that only makes
        sense for another user can't blow up this user's render."""
        out = self._render(
            "{% if section:coffee %}\n{% if wat:nope %}\nx\n{% endif %}\n{% endif %}"
        )
        assert out.strip() == ""

    def test_sams_daily_note_loses_the_blocks_she_switched_off(self):
        """The end-to-end point of the whole mechanism."""
        prefs = {
            "daily_note.sections": [
                "pulse", "food", "house", "today", "tasks",
                "email", "whatsapp", "meetings", "notes",
            ],
            "health.track_sleep": False,
            "health.strength_target": 0,
            "health.profile_path": "",
            "daily_note.show_listening": False,
            "daily_note.focus_count": 3,
        }
        sam = render_command("daily-note", "sam", prefs=prefs)
        alex = render_command("daily-note", "alex")

        for absent in ("😴 Sleep & Vitals", "🎵 Listening", "## Coffee",
                       "## Transport", "## Snags", "snag_capture",
                       "strength sessions"):
            assert absent not in sam, f"{absent!r} should be gone for Sam"
            assert absent in alex, f"{absent!r} should still be there for Alex"

        # ...but the sections she does use survive, and no marker leaks.
        assert "🏃 Movement" in sam
        assert "## House" in sam
        assert "{%" not in sam and "{{" not in sam

    def test_no_command_leaks_a_marker_for_any_user(self):
        """Meta-guard: whatever the prefs, nothing ships a raw marker."""
        from app.services import preferences as prefs_service

        empty = {k: ([] if v.type == "list_str" else type(v.default)())
                 for k, v in prefs_service.PREFERENCES.items()}
        full = {k: v.default for k, v in prefs_service.PREFERENCES.items()}
        for prefs in (None, empty, full):
            for user in ("alex", "sam"):
                for filename, body in render_command_set(user, prefs=prefs).items():
                    assert "{%" not in body, f"{filename} leaked a marker ({user})"
                    assert "{{" not in body, f"{filename} leaked a token ({user})"


class TestCommandsEndpoint:
    def _app(self):
        from app.api import v1
        app = FastAPI()
        app.include_router(v1.router)
        return app

    def test_rejects_missing_bearer(self):
        client = TestClient(self._app())
        resp = client.get("/api/v1/commands")
        assert resp.status_code == 401

    def test_rejects_unresolvable_token(self, monkeypatch):
        import app.auth.client_token as ct
        monkeypatch.setattr(ct, "resolve_token_to_user", lambda token: None)

        client = TestClient(self._app())
        resp = client.get(
            "/api/v1/commands", headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    @staticmethod
    def _stub_prefs(monkeypatch, values=None):
        """Stub the endpoint's preference read (unit tier has no Postgres).

        The endpoint renders against the caller's preferences, so it opens a
        session — real behaviour, exercised for real in the db tier. Here we
        only care that the right user's set comes back, so short-circuit both
        the session and the lookup.
        """
        import contextlib

        from app.api import v1
        from app.services import preferences as prefs_service

        @contextlib.contextmanager
        def _session():
            yield None

        monkeypatch.setattr(v1, "get_db", lambda: SimpleNamespace(session=_session))
        monkeypatch.setattr(
            prefs_service,
            "get_all",
            lambda session, user_id: values
            if values is not None
            else {k: v.default for k, v in prefs_service.PREFERENCES.items()},
        )

    def test_returns_sams_set_for_sam(self, monkeypatch):
        import app.auth.client_token as ct

        sam = SimpleNamespace(id=2, name="sam", display_name="Sam")
        monkeypatch.setattr(ct, "resolve_token_to_user", lambda token: sam)
        self._stub_prefs(monkeypatch)

        client = TestClient(self._app())
        resp = client.get(
            "/api/v1/commands", headers={"Authorization": "Bearer whatever"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"] == "sam"
        assert set(body["commands"]) == {f"{c}.md" for c in CURATED_COMMANDS}
        assert "vault/Daily Notes/YYYY-MM-DD.md" in body["commands"]["daily-note.md"]
        assert "Sam's workspace" in body["claude_md"]

    def test_returns_alexs_set_for_alex(self, monkeypatch):
        import app.auth.client_token as ct

        alex = SimpleNamespace(id=1, name="alex", display_name="Alex")
        monkeypatch.setattr(ct, "resolve_token_to_user", lambda token: alex)
        self._stub_prefs(monkeypatch)

        client = TestClient(self._app())
        resp = client.get(
            "/api/v1/commands", headers={"Authorization": "Bearer whatever"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"] == "alex"
        assert "vault/Daily Notes/Alex/YYYY-MM-DD.md" in body["commands"]["daily-note.md"]
        assert "is authoritative for the" in body["claude_md"]


# ---------------------------------------------------------------------------
# 3. Drift test — rendered set must match the committed golden snapshots
# ---------------------------------------------------------------------------

class TestNoDriftFromRegistry:
    @pytest.mark.parametrize("slug", CURATED_COMMANDS)
    def test_snapshot_matches_render(self, slug):
        rendered = render_command_set("alex")[f"{slug}.md"]
        snapshot_path = SNAPSHOT_DIR / f"{slug}.md"

        if os.environ.get("UPDATE_COMMAND_SNAPSHOTS"):
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(rendered, encoding="utf-8")
            pytest.skip(f"snapshot rewritten: {slug}.md")

        assert snapshot_path.exists(), (
            f"tests/snapshots/commands/{slug}.md is missing — regenerate with "
            "UPDATE_COMMAND_SNAPSHOTS=1 pytest"
        )
        assert snapshot_path.read_text(encoding="utf-8") == rendered, (
            f"tests/snapshots/commands/{slug}.md has drifted from the registry "
            f"template (server/backend/app/prompts/templates/{slug}.md.j2). "
            "If the template change is intended, regenerate the snapshot with "
            "UPDATE_COMMAND_SNAPSHOTS=1 pytest and re-deliver with "
            "scripts/render_commands.py."
        )

    def test_meeting_md_is_untouched_by_the_registry(self):
        # meeting.md is explicitly out of the curated set — this just guards
        # against a future accidental inclusion.
        assert "meeting" not in CURATED_COMMANDS


# ---------------------------------------------------------------------------
# 4. Project-split invariants (2026-07-28)
# ---------------------------------------------------------------------------

class TestDayToDayCommandSourcesAreDisjoint:
    """The generated six and the static sixteen must not collide.

    `scripts/render_commands.py` delivers both sets into one directory, so a
    filename claimed by both would mean the static copy silently shadows (or
    is shadowed by) the generated one depending on merge order. The script
    raises on this; this test catches it in CI without running delivery.
    """

    def test_no_filename_claimed_by_both_sources(self):
        generated = {f"{slug}.md" for slug in CURATED_COMMANDS}
        static = {p.name for p in _static_command_paths()}
        assert not (generated & static), (
            "these commands exist as BOTH a template and a static file: "
            f"{sorted(generated & static)} — delete the static copy, the "
            "template is the source of truth for the curated set."
        )

    def test_static_commands_are_not_generated_artifacts(self):
        """A static file carrying the GENERATED header is a mis-move."""
        for path in _static_command_paths():
            head = path.read_text(encoding="utf-8")[:200]
            assert not head.startswith("<!-- GENERATED"), (
                f"commands/{path.name} carries the GENERATED header — it "
                "belongs in app/prompts/templates/ and CURATED_COMMANDS, not "
                "in the static set."
            )

    def test_repo_root_has_no_day_to_day_commands(self):
        """The dev project must not carry day-to-day commands.

        All 22 moved to the vault project (delivered) or `commands/`
        (versioned source). `/sync-docs` is user-level, so the dev project's
        own `.claude/commands/` should be empty or absent.
        """
        stale = sorted((REPO_ROOT / ".claude" / "commands").glob("*.md"))
        assert not stale, (
            "day-to-day commands are back in the dev project's "
            f".claude/commands/: {[p.name for p in stale]} — they belong in "
            "commands/ (static) or app/prompts/templates/ (generated), and "
            "get delivered to vault/.claude/commands/."
        )
