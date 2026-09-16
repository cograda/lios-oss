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
    COMMAND_TABLE,
    CURATED_COMMANDS,
    EVERYONE,
    STATIC,
    STATIC_COMMANDS,
    TEMPLATE,
    TEMPLATE_COMMANDS,
    _apply_conditionals,
    commands_for_user,
    context_for_user,
    render_claude_md,
    render_command,
    render_command_set,
    static_commands_for_user,
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

SHARED = {
    "kickoff", "add-task", "find", "week-ahead",
    "tunetasks", "meeting", "note", "plan-week", "weekly-review",
    "checkin", "harvest", "youdoit",
}
ALEX_ONLY_STATIC = {"finance", "import-finance", "linkedin", "listening"}


class TestCommandTable:
    """Alex's curation of 2026-09-06, pinned. Changing who gets what is a
    one-line edit to `COMMAND_TABLE` *and* a one-line edit here — the test is
    the record that the change was a decision, not a slip."""

    def test_table_is_the_full_curated_set(self):
        assert set(COMMAND_TABLE) == SHARED | ALEX_ONLY_STATIC

    def test_shared_commands_are_templates_for_everyone(self):
        for slug in SHARED:
            spec = COMMAND_TABLE[slug]
            assert spec.users == EVERYONE, slug
            assert spec.source == TEMPLATE, slug

    def test_alex_only_commands_are_static_and_restricted(self):
        for slug in ALEX_ONLY_STATIC:
            spec = COMMAND_TABLE[slug]
            assert spec.users == frozenset({"alex"}), slug
            assert spec.source == STATIC, slug

    def test_sam_gets_the_shared_set_and_none_of_alexs(self):
        sam = set(commands_for_user("sam"))
        assert sam == SHARED
        assert "tunetasks" in sam
        assert not (sam & ALEX_ONLY_STATIC)
        assert static_commands_for_user("sam") == ()
        assert set(render_command_set("sam")) == {f"{s}.md" for s in SHARED}

    def test_alex_gets_everything(self):
        assert set(commands_for_user("alex")) == SHARED
        assert set(static_commands_for_user("alex")) == ALEX_ONLY_STATIC

    def test_unknown_user_is_treated_like_sam(self):
        assert set(commands_for_user("someone-new")) == SHARED
        assert static_commands_for_user("someone-new") == ()

    def test_alex_only_template_cannot_be_rendered_for_sam(self, monkeypatch):
        from app.prompts import commands as mod

        monkeypatch.setitem(
            mod.COMMAND_TABLE, "tunetasks", mod.CommandSpec(frozenset({"alex"}))
        )
        with pytest.raises(ValueError, match="not available to 'sam'"):
            render_command("tunetasks", "sam")
        assert "tunetasks" not in commands_for_user("sam")

    def test_aliases_agree_with_the_table(self):
        assert CURATED_COMMANDS == TEMPLATE_COMMANDS
        assert set(TEMPLATE_COMMANDS) == SHARED
        assert set(STATIC_COMMANDS) == ALEX_ONLY_STATIC


class TestSamRenderCarriesNoAlexLiterals:
    """The point of the fold: every command Sam receives must be *hers*.

    `Daily Notes/Alex` is the path literal that leaked most often; `alexrivers`
    is Alex's laptop account and never belongs in anything delivered to her
    Mac; `vault/Alex/` is the pre-split per-user subfolder she never had.
    Mutation-checked 2026-09-06: putting `Daily Notes/Alex/` back into the
    weekly-review template made this fail on that one file."""

    FORBIDDEN = ("Daily Notes/Alex", "alexrivers", "vault/Alex/")

    @pytest.mark.parametrize("slug", sorted(SHARED))
    def test_no_alex_path_or_account_in_sams_render(self, slug):
        body = render_command(slug, "sam")
        for needle in self.FORBIDDEN:
            assert needle not in body, f"{slug}.md for Sam contains {needle!r}"

    def test_pronoun_tokens_agree_for_both_users(self):
        """/youdoit is the pronoun-heavy one. Alex's render is the pre-fold
        text; Sam's must use the right forms, including the standalone
        possessive ("the call at the end is hers", not "her")."""
        alex = render_command("youdoit", "alex")
        sam = render_command("youdoit", "sam")
        assert "taking he means" in alex and "the call at the end is his." in alex
        assert "taking she means" in sam and "the call at the end is hers." in sam
        assert "what decision it leaves her |" in sam
        assert "Sam marks tasks in the loops app" in sam
        assert "Alex" not in sam

    def test_partner_token_flips(self):
        assert "1:1 with Sam (shared household planning)" in render_command("harvest", "alex")
        assert "1:1 with Alex (shared household planning)" in render_command("harvest", "sam")

    def test_sams_claude_md_lists_her_new_commands(self):
        md = render_claude_md("sam")
        for slug in SHARED:
            assert f"`/{slug}`" in md, f"/{slug} missing from Sam's CLAUDE.md"
        for slug in ALEX_ONLY_STATIC:
            assert f"`/{slug}`" not in md, f"/{slug} is Alex-only"
        for needle in self.FORBIDDEN:
            assert needle not in md


class TestPerUserRendering:
    def test_curated_set_is_the_template_half_of_the_table(self):
        assert set(CURATED_COMMANDS) == SHARED

    def test_alex_and_sam_daily_notes_paths_differ(self):
        alex = render_command_set("alex")
        sam = render_command_set("sam")

        assert "vault/Daily Notes/Alex/YYYY-MM-DD.md" in alex["kickoff.md"]
        assert "vault/Daily Notes/YYYY-MM-DD.md" in sam["kickoff.md"]
        # Sam's render must not carry Alex's per-user subfolder literal.
        assert "Daily Notes/Alex/" not in sam["kickoff.md"]

    def test_display_name_substituted(self):
        alex = render_command_set("alex")
        sam = render_command_set("sam")

        assert "Morning kickoff for Alex:" in alex["kickoff.md"]
        assert "Morning kickoff for Sam:" in sam["kickoff.md"]

    def test_lock_in_is_gone(self):
        """Retired 2026-09-03: `/tunetasks` locks in inline (its final step)
        and the Reminders sync is inline there too. Alex ruled Sam's set is
        not a reason to keep a separate command — she can be reinstalled from
        scratch."""
        assert "lock-in" not in CURATED_COMMANDS
        assert "lock-in.md" not in render_command_set("sam")

    def test_triage_is_retired(self):
        """Retired 2026-09-07: folded into `/kickoff`'s intake pass (Track C)."""
        assert "triage" not in CURATED_COMMANDS
        assert "triage.md" not in render_command_set("sam")
        assert "triage.md" not in render_command_set("alex")

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

    def test_sams_kickoff_loses_the_blocks_she_switched_off(self):
        """The end-to-end point of the whole mechanism."""
        prefs = {
            "daily_note.sections": ["consumables", "calendar", "tasks", "notes"],
            "daily_note.show_listening": False,
            "daily_note.show_freshness": False,
            "daily_note.focus_count": 3,
        }
        sam = render_command("kickoff", "sam", prefs=prefs)
        alex = render_command("kickoff", "alex")

        for absent in ("rendered.pulse", "rendered.coffee", "rendered.transport",
                       "rendered.snags", "rendered.listening", "rendered.freshness"):
            assert absent not in sam, f"{absent!r} should be gone for Sam"
            assert absent in alex, f"{absent!r} should still be there for Alex"

        # ...but the section she does use survives, and no marker leaks.
        assert "rendered.consumables" in sam
        assert "rendered.calendar" in sam
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


class TestFreshnessIsServerRendered:
    """Since the 2026-09-07 kickoff redesign, the freshness table's rendering
    rules (which rows come first, how `alerts.unmeasured` is surfaced, never
    marking an unmeasured source as fresh) live server-side in
    `system_daily_brief(render=true)`'s `freshness` fragment — the model
    pastes `rendered.freshness` rather than composing the table itself.
    What the template still owns is only the gate: whether the section is
    requested and rendered at all.
    """

    def test_show_freshness_on_includes_the_fragment(self):
        rendered = render_command("kickoff", "alex")
        assert "rendered.freshness" in rendered

    def test_show_freshness_off_drops_the_fragment(self):
        prefs = {"daily_note.show_freshness": False}
        rendered = render_command("kickoff", "alex", prefs=prefs)
        assert "rendered.freshness" not in rendered


class TestKickoffVerification:
    """Fortnight plan R1 / Backlog: "The daily-note briefing has no
    verification step — seven false claims in one run". The 2026-09-07
    kickoff redesign changes *how* the fix holds: instead of requiring the
    composing model to tag every claim with a jq-path marker, almost the
    whole note is now `system_daily_brief(render=true)`'s pre-sourced
    `rendered` fragments, pasted verbatim — the rule holds by construction
    rather than by an inline marker the model could forget. What's left to
    check is that prior notes are still explicitly ruled out as a data
    source, and that the deterministic bits (vault guard, empty Focus) are
    still wired in.
    """

    def test_prohibits_prior_notes_as_a_data_source(self):
        rendered = render_command("kickoff", "alex")
        assert "not a data source" in rendered.lower()
        assert "prior daily notes" in rendered.lower()

    def test_claims_carry_their_source_holds_by_construction(self):
        rendered = render_command("kickoff", "alex")
        assert "by construction" in rendered

    def test_checkin_also_forbids_the_note_as_a_source(self):
        checkin_text = render_command("checkin", "alex")
        assert "not a data source" in checkin_text.lower()

    def test_empty_focus_renders_explicitly(self):
        rendered = render_command("kickoff", "alex")
        assert "tunetasks" in rendered
        assert "No Focus items set" in rendered

    def test_empty_focus_wording_matches_the_pinned_code_constant(self):
        from app.integrations.system.note_render import EMPTY_FOCUS_LINE

        rendered = render_command("kickoff", "alex")
        assert EMPTY_FOCUS_LINE in rendered

    def test_vault_guard_wired_into_kickoff(self):
        rendered = render_command("kickoff", "alex")
        assert "vault_guard.py" in rendered
        assert "## Vault guard" in rendered

    def test_vault_guard_wired_into_checkin(self):
        checkin_text = render_command("checkin", "alex")
        assert "vault_guard.py" in checkin_text
        assert "## Vault guard" in checkin_text

    def test_kickoff_never_invokes_tunetasks_directly(self):
        """Phase 3 suggests `/tunetasks`; it must not chain into it — that
        would recreate the slow single-session problem this redesign exists
        to fix."""
        rendered = render_command("kickoff", "alex")
        assert "do not run `/tunetasks`" in rendered.lower() or \
            "does not run `/tunetasks`" in rendered.lower() or \
            "do not chain" in rendered.lower()

    def test_checkin_never_invokes_tunetasks(self):
        checkin_text = render_command("checkin", "alex")
        assert "never invokes" in checkin_text.lower() or \
            "never invoke" in checkin_text.lower() or \
            "never reinvoke" in checkin_text.lower() or \
            "wasted churn" in checkin_text.lower()


class TestNoteRenderHelpers:
    """Deterministic pre-processing the templates defer to (R1 exit check):
    the empty-Focus line and the vault-guard section formatter. Pure
    functions, no session, no capability wiring — see the module docstring
    for why.
    """

    def test_empty_list_renders_the_pinned_empty_line(self):
        from app.integrations.system.note_render import (
            EMPTY_FOCUS_LINE,
            focus_section,
        )

        assert focus_section([]) == EMPTY_FOCUS_LINE

    def test_nonempty_list_renders_checkboxes_not_the_empty_line(self):
        from app.integrations.system.note_render import (
            EMPTY_FOCUS_LINE,
            focus_section,
        )

        out = focus_section(["Call the SSE meter line", "Reply to Paddy"])
        assert out != EMPTY_FOCUS_LINE
        assert "- [ ] Call the SSE meter line" in out
        assert "- [ ] Reply to Paddy" in out

    def test_guard_section_empty_on_clean_exit(self):
        from app.integrations.system.note_render import vault_guard_section

        assert vault_guard_section("✓ vault guard: no sync conflicts, no zero-byte notes", 0) == ""

    def test_guard_section_renders_findings_verbatim_on_exit_1(self):
        from app.integrations.system.note_render import vault_guard_section

        stdout = (
            "⚠️  VAULT GUARD — needs attention\n\n"
            "1 zero-byte note(s):\n"
            "  • Projects/lios/Plans/Shipped/split-vault-and-dev-projects.md\n"
        )
        out = vault_guard_section(stdout, 1)
        assert out.startswith("## Vault guard")
        assert "split-vault-and-dev-projects.md" in out


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
        assert set(body["commands"]) == {f"{c}.md" for c in commands_for_user("sam")}
        assert "tunetasks.md" in body["commands"]
        assert not ({f"{c}.md" for c in ALEX_ONLY_STATIC} & set(body["commands"]))
        assert "vault/Daily Notes/YYYY-MM-DD.md" in body["commands"]["kickoff.md"]
        assert "Daily Notes/Alex" not in body["commands"]["weekly-review.md"]
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
        assert "vault/Daily Notes/Alex/YYYY-MM-DD.md" in body["commands"]["kickoff.md"]
        assert "is authoritative for the" in body["claude_md"]


# ---------------------------------------------------------------------------
# 3. Drift test — rendered set must match the committed golden snapshots
# ---------------------------------------------------------------------------

class TestNoDriftFromRegistry:
    """Alex's render of every template equals its committed golden snapshot.

    For the eight commands folded in on 2026-09-06 the snapshot IS the pre-fold
    static file (`git mv commands/<slug>.md tests/snapshots/commands/<slug>.md`),
    so this test is also the proof that folding changed nothing for Alex
    beyond the GENERATED header and the tokens — any deliberate rewording
    shows up as a diff on the snapshot file in that PR, and nowhere else.
    """

    @pytest.mark.parametrize("slug", TEMPLATE_COMMANDS)
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

    def test_every_template_command_has_a_template_file(self):
        templates_dir = REPO_ROOT / "server" / "backend" / "app" / "prompts" / "templates"
        on_disk = {p.name[: -len(".md.j2")] for p in templates_dir.glob("*.md.j2")}
        assert on_disk == set(TEMPLATE_COMMANDS), (
            "templates/ and COMMAND_TABLE disagree: "
            f"only on disk {sorted(on_disk - set(TEMPLATE_COMMANDS))}, "
            f"only in table {sorted(set(TEMPLATE_COMMANDS) - on_disk)}"
        )


# ---------------------------------------------------------------------------
# 4. Project-split invariants (2026-07-28)
# ---------------------------------------------------------------------------

class TestDayToDayCommandSourcesAreDisjoint:
    """The templates and the static files must not collide, and the static
    files must be exactly the table's STATIC rows.

    `scripts/render_commands.py` delivers both sets into one directory, so a
    filename claimed by both would mean the static copy silently shadows (or
    is shadowed by) the generated one depending on merge order. The script
    raises on this; this test catches it in CI without running delivery.
    """

    def test_no_filename_claimed_by_both_sources(self):
        generated = {f"{slug}.md" for slug in TEMPLATE_COMMANDS}
        static = {p.name for p in _static_command_paths()}
        assert not (generated & static), (
            "these commands exist as BOTH a template and a static file: "
            f"{sorted(generated & static)} — delete the static copy, the "
            "template is the source of truth for the curated set."
        )

    def test_static_files_are_exactly_the_tables_static_rows(self):
        """A static file the table does not know about would be delivered to
        nobody (the script iterates the table); a STATIC row with no file
        would fail delivery. Both are caught here, before delivery runs.

        One documented exception: `commands/linkedin.md` is dropped by
        `deploy/release_manifest.py`'s NEVER_SHIP (a personal-brand workflow,
        excluded since July) so it is absent on a checkout built from the
        public lios-oss tree, even though the table still carries the row —
        the row itself is fine to ship, only the file isn't. Any other
        mismatch is still a real error.
        """
        static_files = {p.stem for p in _static_command_paths()}
        allowed_missing = {"linkedin"} - static_files
        assert static_files | allowed_missing == set(STATIC_COMMANDS), (
            f"commands/*.md on disk {sorted(static_files)} != COMMAND_TABLE's "
            f"STATIC rows {sorted(STATIC_COMMANDS)} — add the row or move the file."
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


def test_sam_claude_md_explains_the_folder_scoped_grant():
    """2026-09-06: Sam holds a read-only grant on Alex's `Household/` and
    `People/`. A grant she is never told about is a grant nobody uses."""
    from app.prompts.commands import render_claude_md

    md = render_claude_md("sam", "Sam")
    assert 'as_user="alex"' in md
    assert "`Household/`" in md and "`People/`" in md
    assert "read-only" in md
