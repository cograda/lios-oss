"""Tests for the `household` integration — Domains (Phase B) and the capture
inbox (Phase A2/A4), see
vault/Projects/lios/Plans/household-ops-and-loops-2026-08.md.

Two tiers, per the project convention (tests/conftest.py):

  - Unit tier (default, no DB): model shape (no `UserOwnedMixin` — this is a
    household-shared, single-owner table, not a per-user one), manifest
    sanity, that every tool carries inline MCP annotations, and pure keyword
    parsing (`capture.parse_capture_text` needs no DB at all).
  - `db` tier (real Postgres, skipped by `-m "not db"`): each tool's happy
    path, plus the behaviours the task most cares about getting right —
    domains are visible to BOTH users regardless of which one owns them, the
    standard-signal self-report guardrail, capture idempotency (including
    the fixed `snags`-style trap: a manual capture must not get duplicated
    by a later WhatsApp scan of the same content), and sender attribution.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user

# ---------------------------------------------------------------------------
# Unit tier — no DB
# ---------------------------------------------------------------------------


class TestModelShape:
    def test_domain_is_household_shared_not_user_owned(self):
        """Domain must NOT carry UserOwnedMixin's `user_id` column — that
        mixin scopes *visibility*, and both household members must see
        every domain regardless of who owns it. Ownership is expressed via
        the plain `owner_id` FK instead (see models.py's module docstring)."""
        from app.integrations.household.models import Domain

        assert not hasattr(Domain, "user_id")
        assert hasattr(Domain, "owner_id")

    def test_domain_check_is_also_household_shared(self):
        from app.integrations.household.models import DomainCheck

        assert not hasattr(DomainCheck, "user_id")
        assert hasattr(DomainCheck, "checked_by_id")

    def test_manifest_shape(self):
        from app.integrations.household.manifest import MANIFEST

        assert MANIFEST.name == "household"
        assert MANIFEST.type == "capability"
        assert set(MANIFEST.models) == {
            "Domain", "DomainCheck", "HouseholdCapture", "HouseholdCaptureSourceMessage",
        }
        assert MANIFEST.schedule is None
        assert MANIFEST.staleness_probe is None

    def test_manifest_name_matches_integration_class(self):
        from app.integrations.household import HouseholdIntegration

        assert HouseholdIntegration().name == "household"


class TestToolSurface:
    def _tools(self):
        from app.integrations.household.tools import mcp_tools
        return mcp_tools()

    def test_ten_tools_registered(self):
        names = {t["name"] for t in self._tools()}
        assert names == {
            "household_domains_list",
            "household_domain_get",
            "household_domain_add",
            "household_domain_update",
            "household_domain_check",
            "household_domains_render",
            "household_capture_capture",
            "household_capture_add",
            "household_capture_list",
            "household_capture_review",
        }

    def test_every_tool_carries_annotations(self):
        for tool in self._tools():
            assert tool.get("annotations"), f"{tool['name']} missing annotations"

    def test_read_tools_are_read_only(self):
        by_name = {t["name"]: t for t in self._tools()}
        for name in ("household_domains_list", "household_domain_get"):
            assert by_name[name]["annotations"]["readOnlyHint"] is True

    def test_write_tools_are_not_read_only(self):
        by_name = {t["name"]: t for t in self._tools()}
        for name in (
            "household_domain_add", "household_domain_update", "household_domain_check",
            "household_domains_render",
        ):
            assert by_name[name]["annotations"]["readOnlyHint"] is False


# ---------------------------------------------------------------------------
# db tier — real Postgres
# ---------------------------------------------------------------------------


def _add(session, **overrides):
    from app.integrations.household.tools import household_domain_add_handler

    args = {
        "name": "laundry",
        "owner": "sam",
        "operational_definition": "Gather, sort, wash, dry, tidy, fold, iron, put away, plus dry cleaning.",
        "scope_note": "Any clothing anywhere in the house that isn't where it lives.",
    }
    args.update(overrides)
    with use_user(1):
        out = household_domain_add_handler(session, args)
    return json.loads(out)


@pytest.mark.db
class TestDomainAdd:
    def test_add_happy_path(self, db_session):
        result = _add(db_session, name="laundry-1", owner="sam")
        assert result["created"]["name"] == "laundry-1"
        assert result["created"]["owner"] == "sam"
        assert result["created"]["checklist"] == []

    def test_add_missing_required_field_errors(self, db_session):
        from app.integrations.household.tools import household_domain_add_handler

        with use_user(1):
            out = household_domain_add_handler(db_session, {"name": "bins-1", "owner": "sam"})
        result = json.loads(out)
        assert result["status"] == "error"

    def test_add_unknown_owner_errors(self, db_session):
        result = _add(db_session, name="bins-2", owner="not-a-real-user")
        assert result["status"] == "error"

    def test_add_duplicate_name_errors(self, db_session):
        _add(db_session, name="dishwasher-1", owner="sam")
        result = _add(db_session, name="dishwasher-1", owner="sam")
        assert result["status"] == "error"

    def test_checklist_must_be_list_of_strings(self, db_session):
        result = _add(db_session, name="toilets-1", owner="sam", checklist="not a list")
        assert result["status"] == "error"


@pytest.mark.db
class TestDomainsVisibleToBothUsers:
    def test_domain_owned_by_one_user_is_visible_to_the_other(self, db_session):
        """The core household-shared/single-owner behaviour: a domain owned
        by user 1 (alex) must still appear when user 2 (sam) lists
        domains — visibility is NOT scoped by owner_id."""
        from app.integrations.household.tools import household_domains_list_handler

        _add(db_session, name="dishwasher-2", owner="alex")

        with use_user(2):
            out = household_domains_list_handler(db_session, {})
        result = json.loads(out)

        names = {d["name"] for d in result["results"]}
        assert "dishwasher-2" in names

        owned = next(d for d in result["results"] if d["name"] == "dishwasher-2")
        assert owned["owner"] == "alex"

    def test_domain_get_by_the_non_owner_succeeds(self, db_session):
        """A read by the non-owning user must work too — visibility, not
        just listing, is household-shared."""
        from app.integrations.household.tools import household_domain_get_handler

        _add(db_session, name="bins-3", owner="alex")

        with use_user(2):
            out = household_domain_get_handler(db_session, {"name": "bins-3"})
        result = json.loads(out)
        assert result["name"] == "bins-3"
        assert result["owner"] == "alex"

    def test_owner_filter_narrows_the_shared_list(self, db_session):
        from app.integrations.household.tools import household_domains_list_handler

        _add(db_session, name="laundry-3", owner="sam")
        _add(db_session, name="bins-4", owner="alex")

        with use_user(1):
            out = household_domains_list_handler(db_session, {"owner": "sam"})
        result = json.loads(out)
        names = {d["name"] for d in result["results"]}
        assert "laundry-3" in names
        assert "bins-4" not in names


@pytest.mark.db
class TestDomainUpdate:
    def test_update_operational_definition(self, db_session):
        from app.integrations.household.tools import household_domain_update_handler

        _add(db_session, name="toilets-2", owner="sam")
        with use_user(2):
            out = household_domain_update_handler(
                db_session,
                {"name": "toilets-2", "operational_definition": "Clean weekly, restock supplies."},
            )
        result = json.loads(out)
        assert result["updated"]["operational_definition"] == "Clean weekly, restock supplies."

    def test_update_reassigns_owner(self, db_session):
        from app.integrations.household.tools import household_domain_update_handler

        _add(db_session, name="bins-5", owner="sam")
        with use_user(1):
            out = household_domain_update_handler(db_session, {"name": "bins-5", "owner": "alex"})
        result = json.loads(out)
        assert result["updated"]["owner"] == "alex"

    def test_update_no_recognised_fields_errors(self, db_session):
        from app.integrations.household.tools import household_domain_update_handler

        _add(db_session, name="bins-6", owner="sam")
        with use_user(1):
            out = household_domain_update_handler(db_session, {"name": "bins-6"})
        result = json.loads(out)
        assert result["status"] == "error"

    def test_update_cannot_write_the_standard_signal(self, db_session):
        """household_domain_update must never write standard_note /
        standard_updated_at — that's household_domain_check's job alone
        (self-report only). Passing standard_note here is simply not a
        recognised field and is silently ignored (not an error, since
        `operational_definition` is also being changed in the same call)."""
        from app.integrations.household.tools import household_domain_update_handler

        _add(db_session, name="laundry-4", owner="sam")
        with use_user(1):
            out = household_domain_update_handler(
                db_session,
                {
                    "name": "laundry-4",
                    "cadence": "weekly",
                    "standard_note": "not to standard",  # not a recognised field
                },
            )
        result = json.loads(out)
        assert result["updated"]["standard_note"] is None
        assert "standard_note" not in result["changed"]


@pytest.mark.db
class TestStandardSignalSelfReportOnly:
    def test_owner_can_check_in(self, db_session):
        from app.integrations.household.tools import household_domain_check_handler

        _add(db_session, name="dishwasher-3", owner="sam")
        with use_user(2):  # sam is the owner
            out = household_domain_check_handler(
                db_session, {"name": "dishwasher-3", "note": "all good this week"},
            )
        result = json.loads(out)
        assert result["checked"]["standard_note"] == "all good this week"
        assert result["checked"]["standard_updated_at"] is not None

    def test_non_owner_cannot_check_in(self, db_session):
        """The single most important guardrail in this package: user 1
        (alex) must NOT be able to write a standard-signal check-in against
        a domain owned by user 2 (sam) — the plan's own §Social risk
        warning against one spouse flagging the other's domain."""
        from app.integrations.household.tools import household_domain_check_handler

        _add(db_session, name="laundry-5", owner="sam")
        with use_user(1):  # alex, NOT the owner
            out = household_domain_check_handler(
                db_session, {"name": "laundry-5", "note": "not to standard"},
            )
        result = json.loads(out)
        assert result["status"] == "error"
        assert "self-report only" in result["detail"]

        # And the domain's standard fields must be untouched.
        from app.integrations.household.tools import household_domain_get_handler

        with use_user(1):
            get_out = household_domain_get_handler(db_session, {"name": "laundry-5"})
        get_result = json.loads(get_out)
        assert get_result["standard_note"] is None
        assert get_result["standard_updated_at"] is None

    def test_check_in_never_computes_a_streak_or_overdue_flag(self, db_session):
        """Design constraint 1: a skipped cadence must never accumulate
        visible guilt. Assert the response shape carries no such field at
        all — there is no streak/overdue/score key to accidentally rely on."""
        from app.integrations.household.tools import household_domain_check_handler

        _add(db_session, name="bins-7", owner="sam")
        with use_user(2):
            out = household_domain_check_handler(db_session, {"name": "bins-7"})
        result = json.loads(out)
        checked = result["checked"]
        for forbidden_key in ("streak", "overdue", "score", "missed_count", "on_time"):
            assert forbidden_key not in checked

    def test_recent_checks_appear_in_domain_get(self, db_session):
        from app.integrations.household.tools import (
            household_domain_check_handler, household_domain_get_handler,
        )

        _add(db_session, name="toilets-3", owner="sam")
        with use_user(2):
            household_domain_check_handler(db_session, {"name": "toilets-3", "note": "cleaned"})

        with use_user(1):
            out = household_domain_get_handler(db_session, {"name": "toilets-3"})
        result = json.loads(out)
        assert len(result["recent_checks"]) == 1
        assert result["recent_checks"][0]["checked_by"] == "sam"
        assert result["recent_checks"][0]["note"] == "cleaned"


@pytest.fixture
def real_vault_root(tmp_path, monkeypatch):
    """Domains vault rendering needs a real directory for the calling user's
    vault instead of the default `/vaults`, which doesn't exist on a test
    runner. Mirrors tests/test_snag_scoping.py's `_real_vault_root` fixture."""
    from app.config import settings

    (tmp_path / "alex").mkdir()
    (tmp_path / "sam").mkdir()
    monkeypatch.setattr(settings, "vaults_root_path", str(tmp_path))
    return tmp_path


@pytest.mark.db
class TestDomainsVaultRender:
    """Household/Domains.md — a generated, one-way view of the domains
    table. See render.py's module docstring for the two behaviours this
    class exists to prove: byte-idempotent no-op writes, and the
    unreported/not-to-standard distinction (Design constraint 1)."""

    def _note_path(self, tmp_path, user="alex"):
        return tmp_path / user / "Household" / "Domains.md"

    def test_add_renders_the_note(self, db_session, real_vault_root):
        _add(db_session, name="laundry-render-1", owner="sam")
        note = self._note_path(real_vault_root)
        assert note.exists()
        content = note.read_text(encoding="utf-8")
        assert "laundry-render-1" in content
        assert "do not hand-edit" in content

    def test_unreported_domain_never_renders_as_not_to_standard(self, db_session, real_vault_root):
        """Design constraint 1: a domain with no self-report yet must render
        as UNREPORTED, never as a failing/not-to-standard state — the two
        are different facts and must not be conflated into one badge."""
        _add(db_session, name="bins-render-1", owner="sam")
        content = self._note_path(real_vault_root).read_text(encoding="utf-8")

        section = content.split("## bins-render-1", 1)[1].split("## ", 1)[0]
        assert "UNREPORTED" in section
        assert "not to standard" not in section.lower()
        assert "not currently to standard" not in section.lower()

    def test_owner_reported_not_to_standard_renders_their_words_not_a_failing_badge(
        self, db_session, real_vault_root,
    ):
        """Once the owner DOES self-report "not to standard", that text
        renders as their own words (self-report), not escalated into a
        system-asserted failing state — still a report, not a verdict."""
        from app.integrations.household.tools import household_domain_check_handler

        _add(db_session, name="toilets-render-1", owner="sam")
        with use_user(2):  # sam is the owner — the render happens into
            # sam's own vault, since the render call is made with whatever
            # user is currently active (matches the `snags` precedent: a
            # household-shared register renders into the calling user's
            # personal vault, not a shared one — single-user vaults were
            # retired 2026-06-05).
            household_domain_check_handler(
                db_session, {"name": "toilets-render-1", "note": "not currently to standard"},
            )
        content = self._note_path(real_vault_root, user="sam").read_text(encoding="utf-8")
        section = content.split("## toilets-render-1", 1)[1].split("## ", 1)[0]
        assert "UNREPORTED" not in section
        assert "not currently to standard" in section

    def test_rerender_with_no_change_does_not_touch_mtime(self, db_session, real_vault_root):
        """The vault echo-loop lesson: re-rendering byte-identical content
        must not touch the file's mtime, or the vault watcher's fsevents
        re-embed loop fires for a file that never actually changed."""
        from app.integrations.household.tools import household_domains_render_handler

        _add(db_session, name="dishwasher-render-1", owner="alex")
        note = self._note_path(real_vault_root)
        first_mtime = note.stat().st_mtime_ns

        with use_user(1):
            out = household_domains_render_handler(db_session, {})
        assert json.loads(out)["rendered"] == "Household/Domains.md"

        second_mtime = note.stat().st_mtime_ns
        assert second_mtime == first_mtime, "no-op re-render must not touch mtime"

    def test_content_change_does_touch_mtime(self, db_session, real_vault_root):
        """The flip side of the idempotency test: a genuine content change
        must still result in a real write, or the whole test above would be
        meaningless (it could pass because writes never happen at all)."""
        from app.integrations.household.tools import household_domain_update_handler

        _add(db_session, name="dishwasher-render-2", owner="alex")
        note = self._note_path(real_vault_root)
        before_content = note.read_text(encoding="utf-8")

        with use_user(1):
            household_domain_update_handler(
                db_session, {"name": "dishwasher-render-2", "cadence": "daily"},
            )

        after_content = note.read_text(encoding="utf-8")
        assert after_content != before_content
        assert "daily" in after_content

    def test_forced_render_tool_is_available(self, db_session, real_vault_root):
        from app.integrations.household.tools import household_domains_render_handler

        _add(db_session, name="bins-render-2", owner="sam")
        with use_user(1):
            out = household_domains_render_handler(db_session, {})
        result = json.loads(out)
        assert result["rendered"] == "Household/Domains.md"


# ---------------------------------------------------------------------------
# Capture inbox — Phase A2/A4, unit tier (pure parsing, no DB)
# ---------------------------------------------------------------------------


class TestCaptureModelShape:
    def test_capture_is_user_owned_not_household_shared(self):
        """The opposite scoping decision to Domain: a capture belongs to its
        sender, so HouseholdCapture DOES carry UserOwnedMixin's user_id."""
        from app.integrations.household.models import HouseholdCapture

        assert hasattr(HouseholdCapture, "user_id")

    def test_source_message_table_is_also_user_owned(self):
        from app.integrations.household.models import HouseholdCaptureSourceMessage

        assert hasattr(HouseholdCaptureSourceMessage, "user_id")
        assert hasattr(HouseholdCaptureSourceMessage, "capture_id")

    def test_manifest_declares_notify_push_dependency(self):
        from app.integrations.household.manifest import MANIFEST

        assert "notify.push" in MANIFEST.depends_on


class TestCaptureKeywordParsing:
    """`parse_capture_text` needs no DB — pure regex over a string."""

    @pytest.mark.parametrize("text,kind,expected", [
        ("Nag, Tupperware drawer", "nag", "Tupperware drawer"),
        ("nag Tupperware drawer", "nag", "Tupperware drawer"),
        ("NAG: Tupperware drawer", "nag", "Tupperware drawer"),
        ("Task - book dentist", "task", "book dentist"),
        ("task book dentist", "task", "book dentist"),
        ("Surface: security cameras", "surface", "security cameras"),
        ("surface bins", "surface", "bins"),
        ("Discuss - after-school activities", "discuss", "after-school activities"),
    ])
    def test_leading_keyword_forms_all_parse(self, text, kind, expected):
        from app.integrations.household.capture import parse_capture_text

        result = parse_capture_text(text)
        assert result == {"kind": kind, "capture_text": expected}

    @pytest.mark.parametrize("text", [
        "I have a task for you",
        "Nagging headache today",
        "Tasked with baking a cake",
        "Let's have a discussion about it",
        "The bins were surfacing again",
        "",
        "nag",  # keyword with nothing after it
        "nag   ",  # only whitespace after
    ])
    def test_non_leading_or_empty_shapes_do_not_parse(self, text):
        from app.integrations.household.capture import parse_capture_text

        assert parse_capture_text(text) is None

    def test_multiline_voice_transcript_captures_everything_after_keyword(self):
        from app.integrations.household.capture import parse_capture_text

        result = parse_capture_text("Nag,\nTupperware drawer\nagain please")
        assert result["kind"] == "nag"
        assert result["capture_text"] == "Tupperware drawer\nagain please"

    def test_case_insensitive_keyword(self):
        from app.integrations.household.capture import parse_capture_text

        assert parse_capture_text("SURFACE bins")["kind"] == "surface"
        assert parse_capture_text("SuRfAcE bins")["kind"] == "surface"


class TestConfiguredCaptureKeywords:
    """`configured_capture_keywords()` — Tranche 2.5's config-driven scan
    watch-list. Unit tier: no DB, `plugin_config` is monkeypatched directly."""

    def _patch(self, monkeypatch, capture_keywords):
        from app.integrations.household import capture as capture_mod

        monkeypatch.setattr(
            capture_mod, "plugin_config",
            lambda name: type("Cfg", (), {"capture_keywords": capture_keywords})(),
        )

    def test_unconfigured_defaults_exclude_nag(self, monkeypatch):
        """The load-bearing default from household-ops-and-loops-2026-08.md's
        open decision 6: nag ships off. An unconfigured deployment (empty
        list, matching a fresh install with no PUT to the config endpoint)
        must fall back to task/discuss/surface/feedback only."""
        from app.integrations.household.capture import configured_capture_keywords

        self._patch(monkeypatch, [])
        assert configured_capture_keywords() == ("task", "discuss", "surface", "feedback")
        assert "nag" not in configured_capture_keywords()

    def test_nag_becomes_available_once_deliberately_configured(self, monkeypatch):
        from app.integrations.household.capture import configured_capture_keywords

        self._patch(monkeypatch, ["task", "nag", "discuss", "surface"])
        assert configured_capture_keywords() == ("task", "nag", "discuss", "surface")

    def test_config_can_narrow_to_a_subset(self, monkeypatch):
        from app.integrations.household.capture import configured_capture_keywords

        self._patch(monkeypatch, ["surface"])
        assert configured_capture_keywords() == ("surface",)

    def test_unknown_configured_value_is_dropped_not_trusted(self, monkeypatch):
        """A typo or a stray sigil in config must not make it into the
        regex alternation unfiltered — it's dropped, and the valid
        remainder still applies."""
        from app.integrations.household.capture import configured_capture_keywords

        self._patch(monkeypatch, ["task", "shout"])
        assert configured_capture_keywords() == ("task",)

    def test_config_that_is_entirely_invalid_falls_back_to_default(self, monkeypatch):
        from app.integrations.household.capture import configured_capture_keywords

        self._patch(monkeypatch, ["$", "&"])
        assert configured_capture_keywords() == ("task", "discuss", "surface", "feedback")

    def test_manifest_default_matches_the_code_default(self):
        """The manifest's declared default and capture.py's
        DEFAULT_CAPTURE_KEYWORDS must not drift apart — one documents the
        config surface, the other is what actually runs when unconfigured."""
        from app.integrations.household.capture import DEFAULT_CAPTURE_KEYWORDS
        from app.integrations.household.manifest import MANIFEST

        assert set(MANIFEST.config_schema["capture_keywords"].default) == set(DEFAULT_CAPTURE_KEYWORDS)
        assert "nag" not in MANIFEST.config_schema["capture_keywords"].default

    def test_manual_capture_add_is_unaffected_by_scan_config(self, monkeypatch):
        """household_capture_add (A4, a live typed/voice conversation) must
        still accept 'nag' even when the scan config excludes it — the
        config key gates the unattended WhatsApp scan only."""
        from app.integrations.household.capture import KINDS

        self._patch(monkeypatch, [])  # scan config excludes nag
        assert "nag" in KINDS  # the manual-add validation set is untouched


class TestCaptureToolSurface:
    def _tools(self):
        from app.integrations.household.tools import mcp_tools
        return {t["name"]: t for t in mcp_tools()}

    def test_capture_tools_present(self):
        tools = self._tools()
        for name in (
            "household_capture_capture", "household_capture_add",
            "household_capture_list", "household_capture_review",
        ):
            assert name in tools

    def test_capture_capture_is_not_read_only(self):
        assert self._tools()["household_capture_capture"]["annotations"]["readOnlyHint"] is False

    def test_capture_list_is_read_only(self):
        assert self._tools()["household_capture_list"]["annotations"]["readOnlyHint"] is True


# ---------------------------------------------------------------------------
# Capture inbox — db tier
# ---------------------------------------------------------------------------


def _make_whatsapp_message(
    session, user_id: int, message_id: str, body: str, *,
    sender_name: str = "Someone", is_from_me: bool = False,
    timestamp: datetime | None = None,
):
    from app.integrations.whatsapp.models import WhatsAppMessage

    msg = WhatsAppMessage(
        user_id=user_id, message_id=message_id, chat_id="chat-1",
        chat_name="Household", sender_id="+353-1", sender_name=sender_name,
        is_group=False, timestamp=timestamp or (datetime.now(timezone.utc) - timedelta(hours=1)),
        message_type="text", body=body, is_from_me=is_from_me,
    )
    session.add(msg)
    session.commit()
    return msg


def _enable_nag_scan(monkeypatch):
    """Opt the WhatsApp *scan* into watching for `nag`, mirroring what a
    household would do via `PUT /api/integrations/household/config` after
    making the deliberate choice household-ops-and-loops-2026-08.md's open
    decision 6 leaves undecided by default. Several tests below exercise
    `nag`-shaped scan behaviour (sender attribution, per-bridge scoping,
    the manual/scan dedup trap, the recurrence window) that predates this
    key becoming config-driven — rather than rewriting them onto a
    default-enabled keyword and losing that coverage, they opt nag in
    explicitly, exactly as a real deployment would."""
    from app.integrations.household import capture as capture_mod

    monkeypatch.setattr(
        capture_mod, "plugin_config",
        lambda name: type("Cfg", (), {
            "capture_keywords": ["task", "nag", "discuss", "surface"],
        })(),
    )


@pytest.mark.db
class TestCaptureAdd:
    def test_add_happy_path(self, db_session):
        from app.integrations.household.tools import household_capture_add_handler

        with use_user(1):
            out = household_capture_add_handler(
                db_session, {"kind": "nag", "capture_text": "Tupperware drawer"},
            )
        result = json.loads(out)
        assert result["created"]["kind"] == "nag"
        assert result["created"]["capture_text"] == "Tupperware drawer"
        assert result["created"]["source"] == "typed"
        assert result["created"]["sender"] == "alex"
        assert result["created"]["reviewed"] is False

    def test_add_defaults_sender_to_caller(self, db_session):
        from app.integrations.household.tools import household_capture_add_handler

        with use_user(2):
            out = household_capture_add_handler(
                db_session, {"kind": "surface", "capture_text": "security cameras"},
            )
        result = json.loads(out)
        assert result["created"]["sender"] == "sam"

    def test_add_can_attribute_to_someone_else(self, db_session):
        from app.integrations.household.tools import household_capture_add_handler

        with use_user(1):
            out = household_capture_add_handler(
                db_session,
                {"kind": "nag", "capture_text": "Tupperware drawer", "sender": "sam"},
            )
        result = json.loads(out)
        assert result["created"]["sender"] == "sam"

    def test_add_rejects_unknown_kind(self, db_session):
        from app.integrations.household.tools import household_capture_add_handler

        with use_user(1):
            out = household_capture_add_handler(
                db_session, {"kind": "nonsense", "capture_text": "x"},
            )
        assert json.loads(out)["status"] == "error"

    def test_add_rejects_empty_capture_text(self, db_session):
        from app.integrations.household.tools import household_capture_add_handler

        with use_user(1):
            out = household_capture_add_handler(db_session, {"kind": "task", "capture_text": ""})
        assert json.loads(out)["status"] == "error"

    def test_add_rejects_unknown_source(self, db_session):
        from app.integrations.household.tools import household_capture_add_handler

        with use_user(1):
            out = household_capture_add_handler(
                db_session, {"kind": "task", "capture_text": "x", "source": "carrier-pigeon"},
            )
        assert json.loads(out)["status"] == "error"


@pytest.mark.db
class TestCaptureListAndReview:
    def _add(self, session, user_id, **overrides):
        from app.integrations.household.tools import household_capture_add_handler

        args = {"kind": "task", "capture_text": "book dentist"}
        args.update(overrides)
        with use_user(user_id):
            out = household_capture_add_handler(session, args)
        return json.loads(out)["created"]

    def test_list_defaults_to_callers_own_captures(self, db_session):
        from app.integrations.household.tools import household_capture_list_handler

        self._add(db_session, 1, capture_text="alex's task")
        self._add(db_session, 2, capture_text="sam's task")

        with use_user(1):
            out = household_capture_list_handler(db_session, {})
        result = json.loads(out)
        texts = {c["capture_text"] for c in result["results"]}
        assert texts == {"alex's task"}

    def test_all_senders_sees_everyone(self, db_session):
        from app.integrations.household.tools import household_capture_list_handler

        self._add(db_session, 1, capture_text="alex's task 2")
        self._add(db_session, 2, capture_text="sam's task 2")

        with use_user(1):
            out = household_capture_list_handler(db_session, {"all_senders": True})
        result = json.loads(out)
        texts = {c["capture_text"] for c in result["results"]}
        assert texts == {"alex's task 2", "sam's task 2"}

    def test_sender_filter_implies_all_senders(self, db_session):
        """Passing `sender` alone (without all_senders=True) must still work
        — filtering to someone else's captures obviously requires crossing
        the caller's own-captures default."""
        from app.integrations.household.tools import household_capture_list_handler

        self._add(db_session, 2, capture_text="sam's task 3")

        with use_user(1):
            out = household_capture_list_handler(db_session, {"sender": "sam"})
        result = json.loads(out)
        texts = {c["capture_text"] for c in result["results"]}
        assert texts == {"sam's task 3"}

    def test_kind_filter(self, db_session):
        from app.integrations.household.tools import household_capture_list_handler

        self._add(db_session, 1, kind="nag", capture_text="nag item")
        self._add(db_session, 1, kind="task", capture_text="task item")

        with use_user(1):
            out = household_capture_list_handler(db_session, {"kind": "nag"})
        result = json.loads(out)
        assert {c["capture_text"] for c in result["results"]} == {"nag item"}

    def test_review_marks_reviewed(self, db_session):
        from app.integrations.household.tools import household_capture_review_handler

        created = self._add(db_session, 1, capture_text="to review")
        with use_user(1):
            out = household_capture_review_handler(db_session, {"ids": [created["id"]]})
        result = json.loads(out)
        assert result["reviewed"] == [created["id"]]

        from app.integrations.household.tools import household_capture_list_handler
        with use_user(1):
            listed = json.loads(household_capture_list_handler(db_session, {}))
        row = next(c for c in listed["results"] if c["id"] == created["id"])
        assert row["reviewed"] is True
        assert row["reviewed_at"] is not None

    def test_review_reports_not_found_ids(self, db_session):
        from app.integrations.household.tools import household_capture_review_handler

        with use_user(1):
            out = household_capture_review_handler(db_session, {"ids": [999999]})
        result = json.loads(out)
        assert result["reviewed"] == []
        assert result["not_found"] == [999999]

    def test_review_rejects_empty_ids(self, db_session):
        from app.integrations.household.tools import household_capture_review_handler

        with use_user(1):
            out = household_capture_review_handler(db_session, {"ids": []})
        assert json.loads(out)["status"] == "error"


@pytest.mark.db
class TestCaptureWhatsAppScan:
    def test_nag_shaped_message_not_captured_with_default_config(self, db_session):
        """End-to-end proof of the Tranche 2.5 default: an unconfigured
        deployment's scan does not pick up 'nag'-shaped WhatsApp messages at
        all — not even as a skipped/not-capture-shaped row that later gets
        no confirmation, but genuinely invisible to the SQL prefilter."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _make_whatsapp_message(db_session, 1, "MSG-0", "Nag, Tupperware drawer", is_from_me=True)

        with use_user(1):
            result = capture_whatsapp_keywords(db_session, since_days=7)

        assert result["messages_seen"] == 0
        assert result["captures_created"] == 0
        assert db_session.query(HouseholdCapture).count() == 0

    def test_nag_shaped_message_captured_once_deliberately_configured(self, db_session, monkeypatch):
        """Enabling `nag` via config is the deliberate household decision
        the plan's open decision 6 leaves undecided by default — once made,
        the scan picks it up exactly like any other keyword."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _enable_nag_scan(monkeypatch)
        _make_whatsapp_message(db_session, 1, "MSG-0b", "Nag, Tupperware drawer", is_from_me=True)

        with use_user(1):
            result = capture_whatsapp_keywords(db_session, since_days=7)

        assert result["captures_created"] == 1
        row = db_session.query(HouseholdCapture).one()
        assert row.kind == "nag"

    def test_leading_keyword_message_is_captured(self, db_session):
        """`nag` is excluded from the default scan config (see
        TestConfiguredCaptureKeywords below), so this uses a
        default-enabled keyword instead of the historical `nag` example."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _make_whatsapp_message(db_session, 1, "MSG-1", "Surface, Tupperware drawer", is_from_me=True)

        with use_user(1):
            result = capture_whatsapp_keywords(db_session, since_days=7)

        assert result["captures_created"] == 1
        assert result["not_capture_shaped"] == 0
        rows = db_session.query(HouseholdCapture).all()
        assert len(rows) == 1
        assert rows[0].kind == "surface"
        assert rows[0].capture_text == "Tupperware drawer"

    def test_keyword_mid_sentence_never_reaches_the_parser(self, db_session):
        """"I have a task for you" doesn't even start with a captured
        keyword, so the coarse SQL prefilter (mirroring `snags`' own
        ILIKE-prefix scan) excludes it before parse_capture_text ever runs."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _make_whatsapp_message(
            db_session, 1, "MSG-2", "I have a task for you later", is_from_me=True,
        )

        with use_user(1):
            result = capture_whatsapp_keywords(db_session, since_days=7)

        assert result["messages_seen"] == 0
        assert result["captures_created"] == 0
        assert db_session.query(HouseholdCapture).count() == 0

    def test_keyword_shaped_prefix_but_not_leading_word_is_skipped(self, db_session):
        """"Tasked with baking a cake" passes the coarse SQL prefilter
        (ILIKE 'task%') since it starts with those literal characters, so
        this exercises the precise regex rejection (no word boundary right
        after "task"), not just the SQL prefilter."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _make_whatsapp_message(
            db_session, 1, "MSG-2b", "Tasked with baking a cake", is_from_me=True,
        )

        with use_user(1):
            result = capture_whatsapp_keywords(db_session, since_days=7)

        assert result["messages_seen"] == 1
        assert result["captures_created"] == 0
        assert result["not_capture_shaped"] == 1
        assert db_session.query(HouseholdCapture).count() == 0

    def test_is_from_me_attributes_to_bridge_owner(self, db_session):
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _make_whatsapp_message(db_session, 1, "MSG-3", "surface bins", is_from_me=True)

        with use_user(1):
            capture_whatsapp_keywords(db_session, since_days=7)

        capture = db_session.query(HouseholdCapture).one()
        assert capture.user_id == 1

    def test_named_sender_resolves_to_matching_user(self, db_session, monkeypatch):
        """A message received (not is_from_me) with sender_name matching a
        real user's name attributes the capture to THAT user, not the
        bridge owner."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _enable_nag_scan(monkeypatch)
        _make_whatsapp_message(
            db_session, 1, "MSG-4", "nag Tupperware drawer",
            sender_name="sam", is_from_me=False,
        )

        with use_user(1):
            capture_whatsapp_keywords(db_session, since_days=7)

        capture = db_session.query(HouseholdCapture).one()
        assert capture.user_id == 2

    def test_unresolvable_sender_falls_back_to_the_other_active_user(self, db_session):
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _make_whatsapp_message(
            db_session, 1, "MSG-5", "task book dentist",
            sender_name="Some Unrelated Contact", is_from_me=False,
        )

        with use_user(1):
            capture_whatsapp_keywords(db_session, since_days=7)

        capture = db_session.query(HouseholdCapture).one()
        assert capture.user_id == 2  # the only other active user

    def test_repeat_scan_same_user_is_idempotent(self, db_session):
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture

        _make_whatsapp_message(db_session, 1, "MSG-6", "discuss holiday plans", is_from_me=True)

        with use_user(1):
            first = capture_whatsapp_keywords(db_session, since_days=7)
            second = capture_whatsapp_keywords(db_session, since_days=7)

        assert first["captures_created"] == 1
        assert second["captures_created"] == 0
        assert second["duplicates_linked"] == 0
        assert db_session.query(HouseholdCapture).count() == 1

    def test_shared_group_message_ref_is_scoped_per_bridge_user(self, db_session, monkeypatch):
        """Mirrors tests/test_snag_scoping.py's per-user message_ref dedupe:
        the same shared-group message_id, ingested by both bridges, must not
        make the second user's scan see it as already-captured."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture, HouseholdCaptureSourceMessage

        _enable_nag_scan(monkeypatch)
        shared_ref = "SHARED-CAPTURE-1"
        _make_whatsapp_message(db_session, 1, shared_ref, "nag Tupperware drawer", is_from_me=True)
        _make_whatsapp_message(db_session, 2, shared_ref, "nag Tupperware drawer", is_from_me=True)

        with use_user(1):
            result1 = capture_whatsapp_keywords(db_session, since_days=7)
        with use_user(2):
            result2 = capture_whatsapp_keywords(db_session, since_days=7)

        assert result1["captures_created"] == 1
        assert result2["captures_created"] == 1, (
            "user 2's scan silently deduped against user 1's source-message row"
        )
        assert db_session.query(HouseholdCapture).count() == 2

        refs = (
            db_session.query(HouseholdCaptureSourceMessage)
            .filter(HouseholdCaptureSourceMessage.message_ref == shared_ref)
            .all()
        )
        assert len(refs) == 2
        assert {r.user_id for r in refs} == {1, 2}

    def test_manual_capture_then_matching_whatsapp_message_is_linked_not_duplicated(
        self, db_session, monkeypatch,
    ):
        """The fixed `snags` trap: a manually-added capture (household_capture_add,
        no message_ref at all) must not get duplicated when a later scan finds
        a WhatsApp message with matching content that was never marked
        consumed. Content-based dedup catches this; message-ref tracking alone
        (the `snags` precedent's actual bug) would not."""
        from app.integrations.household.capture import capture_whatsapp_keywords
        from app.integrations.household.models import HouseholdCapture, HouseholdCaptureSourceMessage
        from app.integrations.household.tools import household_capture_add_handler

        _enable_nag_scan(monkeypatch)
        with use_user(1):
            household_capture_add_handler(
                db_session, {"kind": "nag", "capture_text": "Tupperware drawer"},
            )
        assert db_session.query(HouseholdCapture).count() == 1

        _make_whatsapp_message(db_session, 1, "MSG-7", "Nag, Tupperware drawer", is_from_me=True)

        with use_user(1):
            result = capture_whatsapp_keywords(db_session, since_days=7)

        assert result["captures_created"] == 0, "must link to the existing manual capture, not duplicate"
        assert result["duplicates_linked"] == 1
        assert db_session.query(HouseholdCapture).count() == 1

        # And the message is now recorded as consumed, so a third run is a no-op.
        assert db_session.query(HouseholdCaptureSourceMessage).count() == 1
        with use_user(1):
            third = capture_whatsapp_keywords(db_session, since_days=7)
        assert third["messages_seen"] == 0

    def test_recurring_nag_outside_dedup_window_is_a_new_capture(self, db_session, monkeypatch):
        """A nag that recurs weeks later is a genuine new occurrence, not a
        duplicate of the first one — the dedup window must not swallow it."""
        from app.integrations.household import capture as capture_mod
        from app.integrations.household.models import HouseholdCapture

        _enable_nag_scan(monkeypatch)
        old_message = _make_whatsapp_message(
            db_session, 1, "MSG-8", "nag Tupperware drawer",
            is_from_me=True, timestamp=datetime.now(timezone.utc) - timedelta(days=10),
        )
        with use_user(1):
            capture_mod.capture_whatsapp_keywords(db_session, since_days=30)
        assert db_session.query(HouseholdCapture).count() == 1

        # Push the existing capture's created_at outside the dedup window.
        db_session.query(HouseholdCapture).update(
            {"created_at": datetime.now(timezone.utc) - timedelta(hours=72)}
        )
        db_session.commit()

        _make_whatsapp_message(
            db_session, 1, "MSG-9", "nag Tupperware drawer", is_from_me=True,
        )
        with use_user(1):
            result = capture_mod.capture_whatsapp_keywords(db_session, since_days=30)

        assert result["captures_created"] == 1
        assert db_session.query(HouseholdCapture).count() == 2


@pytest.mark.db
class TestCaptureConfirmationPush:
    def test_whatsapp_scan_sends_a_best_effort_push_per_new_capture(self, db_session, monkeypatch):
        from app.integrations.household.tools import household_capture_capture_handler

        sent = []

        class _FakeNotify:
            def send(self, title, body, severity="warning", user_id=None, **kwargs):
                sent.append((title, body, severity, user_id))
                return True

        import app.plugin.capabilities as capabilities

        monkeypatch.setattr(capabilities, "get_capability", lambda name: _FakeNotify())

        _make_whatsapp_message(db_session, 1, "MSG-10", "surface security cameras", is_from_me=True)

        with use_user(1):
            out = household_capture_capture_handler(db_session, {"since_days": 7})
        result = json.loads(out)

        assert result["captures_created"] == 1
        assert len(sent) == 1
        title, body, severity, user_id = sent[0]
        assert "surface" in title
        assert "security cameras" in body
        assert severity == "recovery"
        # A4 + the routing trap named in Tranche 2.5: this must go to the
        # CAPTURER (user 1, is_from_me=True) via `targets`, never a bare
        # household-wide push (user_id=None -> household_targets).
        assert user_id == 1

    def test_confirmation_routes_to_the_attributed_sender_not_the_bridge_owner(
        self, db_session, monkeypatch,
    ):
        """A message received on Alex's bridge (user 1) but sent by Sam
        (user 2, resolved via sender_name) must push to user 2's device, not
        user 1's — the capturer is the sender, not whichever bridge ingested
        the message. This is the exact household_targets-vs-targets trap
        both plan docs flag: routing on the bridge id instead of the
        resolved sender would silently misdeliver every confirmation for a
        message the OTHER household member sent."""
        from app.integrations.household.tools import household_capture_capture_handler

        sent = []

        class _FakeNotify:
            def send(self, title, body, severity="warning", user_id=None, **kwargs):
                sent.append((title, body, severity, user_id))
                return True

        import app.plugin.capabilities as capabilities

        monkeypatch.setattr(capabilities, "get_capability", lambda name: _FakeNotify())

        _make_whatsapp_message(
            db_session, 1, "MSG-10b", "task book dentist",
            sender_name="sam", is_from_me=False,
        )

        with use_user(1):
            household_capture_capture_handler(db_session, {"since_days": 7})

        assert len(sent) == 1
        assert sent[0][3] == 2  # routed to Sam (the sender), not Alex (the bridge)

    def test_confirmation_never_falls_back_to_household_wide_push(self, db_session, monkeypatch):
        """`notify.push`'s `user_id=None` shape means household-wide
        (`household_targets`), which both plan docs record as deliberately
        one person's device. A capture confirmation must always resolve a
        real sender and pass it — never call `send()` with no `user_id`."""
        from app.integrations.household.tools import _notify_capture_confirmation

        sent = []

        class _FakeNotify:
            def send(self, title, body, severity="warning", user_id=None, **kwargs):
                sent.append(user_id)
                return True

        import app.plugin.capabilities as capabilities

        monkeypatch.setattr(capabilities, "get_capability", lambda name: _FakeNotify())

        _notify_capture_confirmation("task", "book dentist", sender=None)
        assert sent == [], "no sender resolved -> skip the push entirely, never guess household-wide"

    def test_confirmation_push_failure_never_breaks_the_capture(self, db_session, monkeypatch):
        """notify.push must be best-effort — a broken/unconfigured capability
        must not fail the capture that triggered it."""
        from app.integrations.household.tools import household_capture_capture_handler

        import app.plugin.capabilities as capabilities

        def _boom(name):
            raise RuntimeError("capability not registered")

        monkeypatch.setattr(capabilities, "get_capability", _boom)

        _make_whatsapp_message(db_session, 1, "MSG-11", "task book dentist", is_from_me=True)

        with use_user(1):
            out = household_capture_capture_handler(db_session, {"since_days": 7})
        result = json.loads(out)

        assert result["captures_created"] == 1


# ---------------------------------------------------------------------------
# Feedback channel — Wave 2 N4
# ---------------------------------------------------------------------------


class TestFeedbackUnitTier:
    """No DB — feedback's vocabulary/config wiring."""

    def test_feedback_is_a_valid_kind(self):
        from app.integrations.household.capture import KINDS

        assert "feedback" in KINDS

    def test_feedback_ships_in_the_default_scan_keywords(self):
        """Unlike `nag`, feedback carries no social-risk open question — it
        should work out of the box, not require a deliberate config PUT."""
        from app.integrations.household.capture import DEFAULT_CAPTURE_KEYWORDS

        assert "feedback" in DEFAULT_CAPTURE_KEYWORDS

    def test_feedback_parses_like_any_other_keyword(self):
        from app.integrations.household.capture import parse_capture_text

        result = parse_capture_text("Feedback: the loops app is slow")
        assert result == {"kind": "feedback", "capture_text": "the loops app is slow"}

    def test_manifest_declares_feedback_recipient_config_key(self):
        from app.integrations.household.manifest import MANIFEST

        assert "feedback_recipient_user" in MANIFEST.config_schema
        assert MANIFEST.config_schema["feedback_recipient_user"].required is False

    def test_manifest_capture_keywords_default_still_matches_code_default(self):
        """Re-asserts TestConfiguredCaptureKeywords's own drift guard now that
        'feedback' has been added to both sides."""
        from app.integrations.household.capture import DEFAULT_CAPTURE_KEYWORDS
        from app.integrations.household.manifest import MANIFEST

        assert set(MANIFEST.config_schema["capture_keywords"].default) == set(DEFAULT_CAPTURE_KEYWORDS)
        assert "feedback" in MANIFEST.config_schema["capture_keywords"].default


@pytest.mark.db
class TestFeedbackCaptureAdd:
    def test_add_accepts_feedback_kind(self, db_session):
        from app.integrations.household.tools import household_capture_add_handler

        with use_user(2):
            out = household_capture_add_handler(
                db_session, {"kind": "feedback", "capture_text": "the loops app is slow"},
            )
        result = json.loads(out)
        assert result["created"]["kind"] == "feedback"
        assert result["created"]["sender"] == "sam"

    def test_whatsapp_scan_picks_up_feedback_prefix_with_no_config_change(self, db_session):
        """`feedback` is in DEFAULT_CAPTURE_KEYWORDS, so an unconfigured
        household (the normal case — no PUT to capture_keywords) still
        captures 'Feedback: ...' via the WhatsApp scan."""
        from app.integrations.household.tools import household_capture_capture_handler

        _make_whatsapp_message(
            db_session, 1, "MSG-FB-1", "Feedback: the loops app is slow", is_from_me=True,
        )
        with use_user(1):
            out = household_capture_capture_handler(db_session, {"since_days": 7})
        result = json.loads(out)
        assert result["captures_created"] == 1
        assert result["created_items"][0]["kind"] == "feedback"


@pytest.mark.db
class TestFeedbackRecipientResolution:
    def test_configured_recipient_resolves_by_name(self, db_session, monkeypatch):
        from app.integrations.household import tools as tools_mod

        cfg = type("Cfg", (), {"feedback_recipient_user": "sam"})()
        monkeypatch.setattr(tools_mod, "plugin_config", lambda name: cfg)

        recipient = tools_mod._resolve_feedback_recipient(db_session)
        assert recipient is not None
        assert recipient.name == "sam"

    def test_unset_falls_back_to_lowest_id_active_user(self, db_session, monkeypatch):
        """No admin/role column exists on User (see app/models/users.py) — the
        documented fallback is the household's first-created active member,
        i.e. the lowest id, which is alex (seeded id 1)."""
        from app.integrations.household import tools as tools_mod

        cfg = type("Cfg", (), {"feedback_recipient_user": None})()
        monkeypatch.setattr(tools_mod, "plugin_config", lambda name: cfg)

        recipient = tools_mod._resolve_feedback_recipient(db_session)
        assert recipient is not None
        assert recipient.name == "alex"

    def test_unknown_configured_name_falls_back_rather_than_raising(self, db_session, monkeypatch):
        from app.integrations.household import tools as tools_mod

        cfg = type("Cfg", (), {"feedback_recipient_user": "not-a-real-user"})()
        monkeypatch.setattr(tools_mod, "plugin_config", lambda name: cfg)

        recipient = tools_mod._resolve_feedback_recipient(db_session)
        assert recipient is not None
        assert recipient.name == "alex"


@pytest.mark.db
class TestFeedbackNotification:
    def _fake_notify(self, monkeypatch):
        sent = []

        class _FakeNotify:
            def send(self, title, body, severity="warning", user_id=None, **kwargs):
                sent.append((title, body, severity, user_id))
                return True

        import app.plugin.capabilities as capabilities

        monkeypatch.setattr(capabilities, "get_capability", lambda name: _FakeNotify())
        return sent

    def test_feedback_notifies_recipient_not_sender(self, db_session, monkeypatch):
        """The core routing contract: 'that should come to me first' — a
        feedback capture from Sam must push to the configured recipient
        (defaults to alex, id 1), never to Sam herself."""
        from app.integrations.household.tools import household_capture_add_handler

        sent = self._fake_notify(monkeypatch)
        with use_user(2):  # sam files feedback
            household_capture_add_handler(
                db_session, {"kind": "feedback", "capture_text": "the loops app is slow"},
            )
        assert len(sent) == 1
        title, body, severity, user_id = sent[0]
        assert "Sam" in title
        assert "the loops app is slow" in body
        assert user_id == 1  # alex, the fallback recipient — never sam (2)

    def test_recipient_filing_their_own_feedback_still_notifies_and_records(
        self, db_session, monkeypatch,
    ):
        """Alex filing feedback to himself is fine and must still record —
        sender and recipient are allowed to coincide."""
        from app.integrations.household.tools import household_capture_add_handler

        sent = self._fake_notify(monkeypatch)
        with use_user(1):  # alex, who is also the fallback recipient
            out = household_capture_add_handler(
                db_session, {"kind": "feedback", "capture_text": "self-noted snag"},
            )
        assert json.loads(out)["created"]["kind"] == "feedback"
        assert len(sent) == 1
        assert sent[0][3] == 1

    def test_whatsapp_scan_feedback_also_notifies_the_recipient(self, db_session, monkeypatch):
        from app.integrations.household.tools import household_capture_capture_handler

        sent = self._fake_notify(monkeypatch)
        _make_whatsapp_message(
            db_session, 1, "MSG-FB-2", "Feedback: sync is flaky again", is_from_me=True,
        )
        with use_user(1):
            household_capture_capture_handler(db_session, {"since_days": 7})

        # Two pushes land here: the sender's own capture confirmation, plus
        # the recipient notification — both route to user 1 in this case
        # (alex captured it and alex is also the fallback recipient), so
        # assert on titles rather than a single push count.
        titles = [s[0] for s in sent]
        assert any(t.startswith("Feedback from") for t in titles)

    def test_non_feedback_capture_never_triggers_recipient_notification(
        self, db_session, monkeypatch,
    ):
        """Mutation-check guard: a 'nag' must not accidentally take the
        feedback-recipient path. Only `_notify_capture_confirmation`
        (title 'lios: nag captured') should fire, never a 'Feedback from'
        title."""
        from app.integrations.household.tools import household_capture_add_handler

        sent = self._fake_notify(monkeypatch)
        with use_user(2):
            household_capture_add_handler(
                db_session, {"kind": "nag", "capture_text": "Tupperware drawer"},
            )
        assert not any(t.startswith("Feedback from") for t, *_ in sent)

    def test_feedback_notification_failure_never_breaks_the_capture(self, db_session, monkeypatch):
        from app.integrations.household.tools import household_capture_add_handler

        import app.plugin.capabilities as capabilities

        def _boom(name):
            raise RuntimeError("capability not registered")

        monkeypatch.setattr(capabilities, "get_capability", _boom)

        with use_user(2):
            out = household_capture_add_handler(
                db_session, {"kind": "feedback", "capture_text": "the loops app is slow"},
            )
        assert json.loads(out)["created"]["kind"] == "feedback"


@pytest.mark.db
class TestFeedbackVaultRender:
    def _note_path(self, tmp_path, user="sam"):
        return tmp_path / user / "Household" / "Feedback.md"

    def test_add_renders_feedback_note_with_its_own_heading_sender_and_date(
        self, db_session, real_vault_root, monkeypatch,
    ):
        from app.integrations.household.tools import household_capture_add_handler

        import app.plugin.capabilities as capabilities

        class _FakeNotify:
            def send(self, *a, **kw):
                return True

        monkeypatch.setattr(capabilities, "get_capability", lambda name: _FakeNotify())

        with use_user(2):  # sam — renders into sam's own vault
            household_capture_add_handler(
                db_session, {"kind": "feedback", "capture_text": "the loops app is slow"},
            )

        note = self._note_path(real_vault_root)
        assert note.exists()
        content = note.read_text(encoding="utf-8")
        assert "# Feedback" in content
        assert "from Sam" in content
        assert "the loops app is slow" in content
        assert "do not hand-edit" in content

    def test_domains_render_is_untouched_by_a_feedback_capture(
        self, db_session, real_vault_root, monkeypatch,
    ):
        """Feedback gets its OWN note — it must not leak into or trigger a
        write of Household/Domains.md."""
        from app.integrations.household.tools import household_capture_add_handler

        import app.plugin.capabilities as capabilities

        class _FakeNotify:
            def send(self, *a, **kw):
                return True

        monkeypatch.setattr(capabilities, "get_capability", lambda name: _FakeNotify())

        domains_note = real_vault_root / "sam" / "Household" / "Domains.md"
        with use_user(2):
            household_capture_add_handler(
                db_session, {"kind": "feedback", "capture_text": "the loops app is slow"},
            )
        assert not domains_note.exists()
