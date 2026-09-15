"""Tests for the google_docs integration (unit tier — no Postgres, no network).

The weight is deliberately on `markup.py`, because that is where the real
risk lives: the Docs/Drive API calls are three thin wrappers whose behaviour
is Google's, while the two markdown converters are ~440 lines of hand-written
parsing written without a markdown library (none is in requirements.txt).
A silent conversion bug there produces a *plausible-looking* document, which
is the worst failure shape available — hence a case per construct, in both
directions.

Everything touching Google is exercised against fakes shaped like the
`googleapiclient` resource chain, the same approach
`test_google_calendar_update_delete.py` takes.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from app.errors import PermanentError

from app.integrations.google_docs.markup import (
    document_to_markdown,
    end_index,
    markdown_to_html,
)
from app.integrations.google_docs.tools import (
    get_mcp_tools,
    handle_append,
    handle_list,
    handle_read,
    handle_replace,
    handle_write,
)
from app.integrations.google_docs.writer import document_id_from

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# markdown -> HTML
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    def test_headings_map_to_matching_levels(self):
        html = markdown_to_html("# One\n\n### Three")
        assert "<h1>One</h1>" in html
        assert "<h3>Three</h3>" in html

    def test_paragraph_lines_join_into_one_paragraph(self):
        # Markdown treats a single newline as a soft wrap, and a Google Doc
        # rendered with one paragraph per source line reads as broken.
        html = markdown_to_html("first line\nsecond line\n\nnext para")
        assert "<p>first line second line</p>" in html
        assert "<p>next para</p>" in html

    def test_nested_list_nests(self):
        html = markdown_to_html("- top\n  - inner\n- back")
        assert html.count("<ul>") == 2
        assert html.count("</ul>") == 2
        assert html.index("<li>inner") > html.index("<li>top")
        # The inner list must close before the item that returns to level 0.
        assert html.index("</ul>") < html.index("<li>back")

    def test_child_list_is_inside_its_parent_item_not_after_it(self):
        """`<ul><li>a</li><ul>…</ul></ul>` is invalid HTML.

        Most importers indent it anyway, but "most" is not a contract worth
        relying on for a document someone else reads, so the parent `<li>`
        stays open until its child list has closed.
        """
        html = markdown_to_html("- top\n  - inner")
        # Blocks are newline-joined for readability; compare structure only.
        flat = re.sub(r"\s+", "", html[html.index("<body>"):])
        assert "<ul><li>top<ul><li>inner</li></ul></li></ul>" in flat

    def test_every_item_is_closed(self):
        html = markdown_to_html("1. one\n2. two\n   - a\n   - b\n3. three")
        assert html.count("<li>") == html.count("</li>") == 5
        assert html.count("<ol>") == html.count("</ol>") == 1
        assert html.count("<ul>") == html.count("</ul>") == 1

    def test_switching_marker_at_same_indent_starts_a_new_list(self):
        html = markdown_to_html("- bullet\n1. number")
        assert "</ul>" in html
        assert "<ol>" in html
        assert html.index("</ul>") < html.index("<li>number")

    def test_table_becomes_a_real_table_with_header_cells(self):
        html = markdown_to_html("| A | B |\n| --- | --- |\n| 1 | 2 |")
        assert "<table" in html
        assert html.count("<th") == 2
        assert html.count("<td") == 2

    def test_ragged_table_row_is_padded_not_shifted(self):
        # A short row must not shift every later cell one column left.
        html = markdown_to_html("| A | B | C |\n|---|---|---|\n| 1 |\n")
        assert html.count("<td") == 3

    def test_pipe_in_a_paragraph_is_not_a_table(self):
        # The separator line is what makes a table; requiring it is what
        # stops "a | b" in prose becoming a one-row table.
        html = markdown_to_html("costs 5 | 6 per unit")
        assert "<table" not in html

    def test_inline_code_is_not_re_scanned_for_emphasis(self):
        # A complete pair of markers inside the code span, so this fails if
        # code spans are not stashed before emphasis is applied. A single
        # stray `**` would pass either way and prove nothing.
        html = markdown_to_html("run `**not bold**` and `*not italic*` now")
        assert "<strong>" not in html
        assert "<em>" not in html
        assert "**not bold**" in html
        assert "*not italic*" in html

    def test_emphasis_link_and_strikethrough(self):
        html = markdown_to_html("**b** and *i* and ~~s~~ and [x](https://e.invalid)")
        assert "<strong>b</strong>" in html
        assert "<em>i</em>" in html
        assert "<s>s</s>" in html
        assert '<a href="https://e.invalid">x</a>' in html

    def test_html_in_source_is_escaped_not_injected(self):
        html = markdown_to_html("a <script>bad()</script> b")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_fenced_code_block_is_preserved_and_escaped(self):
        html = markdown_to_html("```\nif a < b:\n    go()\n```")
        assert "<pre" in html
        assert "a &lt; b" in html
        # Indentation inside a code block is content, not list nesting.
        assert "<ul>" not in html

    def test_rule_and_blockquote(self):
        html = markdown_to_html("> quoted\n\n---\n\ntail")
        assert "<blockquote>" in html
        assert "<hr>" in html

    def test_title_is_optional_and_emitted_when_given(self):
        assert "<h1>" not in markdown_to_html("body text")
        assert "<h1>Brief</h1>" in markdown_to_html("body text", title="Brief")


# ---------------------------------------------------------------------------
# Google Docs document -> markdown
# ---------------------------------------------------------------------------


def _para(text: str, *, style: str = "NORMAL_TEXT", bullet: dict | None = None,
          text_style: dict | None = None) -> dict:
    paragraph: dict = {
        "elements": [{"textRun": {"content": text, "textStyle": text_style or {}}}],
        "paragraphStyle": {"namedStyleType": style},
    }
    if bullet is not None:
        paragraph["bullet"] = bullet
    return {"paragraph": paragraph}


class TestDocumentToMarkdown:
    def test_heading_style_becomes_hashes(self):
        doc = {"body": {"content": [_para("Scope\n", style="HEADING_2")]}}
        assert document_to_markdown(doc).strip() == "## Scope"

    def test_character_styles_wrap_inside_the_trailing_newline(self):
        # The paragraph-terminating newline is structural; emphasis markers
        # placed outside it would produce "**text\n**" and break rendering.
        doc = {"body": {"content": [_para("bold\n", text_style={"bold": True})]}}
        assert document_to_markdown(doc).strip() == "**bold**"

    def test_link_and_monospace(self):
        doc = {"body": {"content": [
            _para("here\n", text_style={"link": {"url": "https://e.invalid"}}),
            _para("code\n", text_style={"weightedFontFamily": {"fontFamily": "Courier New"}}),
        ]}}
        out = document_to_markdown(doc)
        assert "[here](https://e.invalid)" in out
        assert "`code`" in out

    def test_bullet_vs_numbered_comes_from_the_list_definition(self):
        # Ordered-ness is on doc["lists"], never on the paragraph — a reader
        # that guessed from the paragraph alone would render every list as
        # bullets.
        doc = {
            "lists": {
                "L1": {"listProperties": {"nestingLevels": [{"glyphSymbol": "-"}]}},
                "L2": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}},
            },
            "body": {"content": [
                _para("a\n", bullet={"listId": "L1", "nestingLevel": 0}),
                _para("b\n", bullet={"listId": "L2", "nestingLevel": 0}),
            ]},
        }
        out = document_to_markdown(doc)
        assert "- a" in out
        assert "1. b" in out

    def test_nesting_level_indents(self):
        doc = {
            "lists": {"L1": {"listProperties": {"nestingLevels": [
                {"glyphSymbol": "-"}, {"glyphSymbol": "-"},
            ]}}},
            "body": {"content": [
                _para("top\n", bullet={"listId": "L1", "nestingLevel": 0}),
                _para("deep\n", bullet={"listId": "L1", "nestingLevel": 1}),
            ]},
        }
        assert "  - deep" in document_to_markdown(doc)

    def test_consecutive_list_items_are_not_blank_separated(self):
        # A blank line between items splits one list into several in most
        # renderers, including Obsidian's.
        doc = {
            "lists": {"L1": {"listProperties": {"nestingLevels": [{"glyphSymbol": "-"}]}}},
            "body": {"content": [
                _para("a\n", bullet={"listId": "L1", "nestingLevel": 0}),
                _para("b\n", bullet={"listId": "L1", "nestingLevel": 0}),
            ]},
        }
        assert "- a\n- b" in document_to_markdown(doc)

    def test_table_becomes_a_pipe_table_with_separator(self):
        doc = {"body": {"content": [{"table": {"tableRows": [
            {"tableCells": [{"content": [_para("A\n")]}, {"content": [_para("B\n")]}]},
            {"tableCells": [{"content": [_para("1\n")]}, {"content": [_para("2\n")]}]},
        ]}}]}}
        out = document_to_markdown(doc)
        assert "| A | B |" in out
        assert "| --- | --- |" in out
        assert "| 1 | 2 |" in out

    def test_pipe_inside_a_cell_is_escaped(self):
        doc = {"body": {"content": [{"table": {"tableRows": [
            {"tableCells": [{"content": [_para("a|b\n")]}]},
        ]}}]}}
        assert r"a\|b" in document_to_markdown(doc)

    def test_inline_image_is_named_not_dropped(self):
        doc = {"body": {"content": [{"paragraph": {
            "elements": [{"inlineObjectElement": {"inlineObjectId": "i1"}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        }}]}}
        assert "[image]" in document_to_markdown(doc)

    def test_empty_spacer_paragraphs_collapse(self):
        doc = {"body": {"content": [_para("a\n"), _para("\n"), _para("\n"), _para("b\n")]}}
        assert document_to_markdown(doc) == "a\n\nb\n"


class TestEndIndex:
    def test_insertion_point_is_one_before_the_body_end(self):
        # Inserting *at* endIndex is rejected by the Docs API; this off-by-one
        # is the only index arithmetic in the package.
        assert end_index({"body": {"content": [{"endIndex": 42}]}}) == 41

    def test_empty_document_starts_at_one(self):
        assert end_index({"body": {"content": []}}) == 1


# ---------------------------------------------------------------------------
# Reference normalisation
# ---------------------------------------------------------------------------


class TestDocumentIdFrom:
    @pytest.mark.parametrize("reference", [
        "abc123_DEF-456",
        "https://docs.google.com/document/d/abc123_DEF-456/edit",
        "https://docs.google.com/document/d/abc123_DEF-456/edit?tab=t.0#heading=h.x",
        " https://docs.google.com/document/d/abc123_DEF-456 ",
    ])
    def test_url_or_bare_id_both_resolve(self, reference):
        # Callers paste URLs far more often than ids, and a URL used as an id
        # 404s in a way that reads like a permissions problem.
        assert document_id_from(reference) == "abc123_DEF-456"


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


class TestToolSurface:
    def test_five_tools_all_annotated(self):
        tools = get_mcp_tools()
        assert {tool["name"] for tool in tools} == {
            "docs_read", "docs_write", "docs_append", "docs_replace", "docs_list",
        }
        # Missing annotations fail server startup (MissingAnnotationsError);
        # catching it here is faster than catching it in a deploy.
        for tool in tools:
            assert tool.get("annotations"), tool["name"]

    def test_mutating_tools_are_not_marked_read_only(self):
        by_name = {tool["name"]: tool["annotations"] for tool in get_mcp_tools()}
        assert by_name["docs_read"]["readOnlyHint"] is True
        assert by_name["docs_list"]["readOnlyHint"] is True
        for name in ("docs_write", "docs_append", "docs_replace"):
            assert by_name[name]["readOnlyHint"] is False, name
        # Overwriting existing content is destructive; appending is not.
        assert by_name["docs_write"]["destructiveHint"] is True
        assert by_name["docs_replace"]["destructiveHint"] is True
        assert by_name["docs_append"]["destructiveHint"] is False


class TestWriteHandler:
    def test_empty_markdown_is_refused_before_any_api_call(self):
        # An accidental empty write would blank a shared document, and the
        # Drive update that does it is not reversible from here.
        session = MagicMock()
        result = handle_write(session, {"key": "brief", "markdown": "   "})
        assert "refusing" in result
        session.query.assert_not_called()

    def test_missing_key_is_refused(self):
        assert "key is required" in handle_write(MagicMock(), {"markdown": "x"})

    def test_rewrite_uses_the_callers_token_even_when_another_account_created_it(self):
        """Decision of 2026-09-06: no tool may fall back to another user's
        credentials. Before, this branch deliberately looked up the CREATING
        account's token (the `drive.file` per-file grant argument); now the
        caller's own token does the write, whoever created the document.
        `owner_account_email` stays on the export as a record only."""
        export = MagicMock(key="brief", title="Brief", document_id="d1",
                          document_url="u1", owner_account_email="owner@example.invalid")
        session = MagicMock()
        session.query.return_value.filter_by.return_value.one_or_none.return_value = export

        with patch("app.integrations.google_docs.writer.write_markdown") as write, \
             patch("app.integrations.google_docs.tools._caller_account",
                   return_value=("caller@example.invalid", 2)):
            handle_write(session, {"key": "brief", "markdown": "# hi"})

        assert write.call_args.kwargs["account_email"] == "caller@example.invalid"
        assert write.call_args.kwargs["user_id"] == 2
        # The creating account's token is never looked up — one query only
        # (the DocExport lookup); a second `.first()` would be the owner path.
        session.query.return_value.filter_by.return_value.first.assert_not_called()

    def test_rewrite_without_a_token_of_your_own_is_refused_before_any_api_call(self):
        export = MagicMock(key="brief", owner_account_email="owner@example.invalid")
        session = MagicMock()
        session.query.return_value.filter_by.return_value.one_or_none.return_value = export

        with patch("app.integrations.google_docs.writer.write_markdown") as write, \
             patch("app.integrations.google_docs.tools._caller_account",
                   side_effect=PermanentError("you have no Google account connected")), \
             pytest.raises(PermanentError, match="no Google account connected"):
            handle_write(session, {"key": "brief", "markdown": "# hi"})

        write.assert_not_called()

    def test_creation_records_the_caller_as_the_documents_owner(self):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.one_or_none.return_value = None
        created = MagicMock(key="k", title="k", document_id="d", document_url="u")

        with patch("app.integrations.google_docs.writer.ensure_export", return_value=created) as ensure, \
             patch("app.integrations.google_docs.tools._share_with", return_value=[]), \
             patch("app.integrations.google_docs.tools._caller_account",
                   return_value=("caller@example.invalid", 2)):
            handle_write(session, {"key": "k", "markdown": "# x"})

        assert ensure.call_args.kwargs["owner_account_email"] == "caller@example.invalid"
        assert ensure.call_args.kwargs["owner_user_id"] == 2


class TestListHandler:
    def test_empty_registry_reports_zero(self):
        session = MagicMock()
        session.query.return_value.order_by.return_value.all.return_value = []
        assert '"count": 0' in handle_list(session, {})


class TestFacadeBoundary:
    def test_docs_write_capability_resolves_to_the_facade(self):
        from app.integrations.google_docs.facade import FACADE
        from app.plugin.capabilities import get_capability

        assert get_capability("docs.write") is FACADE


class TestManifest:
    def test_config_keys_are_namespaced_so_env_fallbacks_do_not_collide(self):
        """`plugin_config` derives each key's env fallback as `HOME_<KEY>`.

        A bare `owner_account` would therefore claim the global name
        `HOME_OWNER_ACCOUNT` — generic enough that a later integration
        wanting an owner account would silently share this one's value.
        """
        from app.integrations.google_docs.manifest import MANIFEST

        assert set(MANIFEST.config_schema) == {"docs_owner_account", "docs_share_with"}
        for key in MANIFEST.config_schema:
            assert key.startswith("docs_"), key

    def test_owner_account_is_not_required_config(self):
        # `required` gates the whole integration off via is_configured(), which
        # would take the read paths down with it. The two calls that need an
        # owner account fail at the call site instead.
        from app.integrations.google_docs.manifest import MANIFEST

        assert MANIFEST.config_schema["docs_owner_account"].required is False

    def test_declares_both_scopes_it_actually_uses(self):
        # documents: Docs API read/append/replace on any visible doc.
        # drive.file: create + whole-document overwrite, per-file grant only.
        from app.integrations.google_docs.manifest import MANIFEST

        assert MANIFEST.oauth is not None
        assert set(MANIFEST.oauth.scopes) == {
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.file",
        }
        # A widened Drive grant would remove the per-file limit but hand full
        # Drive access to every integration sharing the scope union.
        assert "https://www.googleapis.com/auth/drive" not in MANIFEST.oauth.scopes

    def test_no_schedule_and_no_staleness_probe(self):
        # Nothing to poll: every call is user- or caller-initiated, so a cron
        # job and a freshness probe would both be measuring nothing.
        from app.integrations.google_docs.manifest import MANIFEST

        assert MANIFEST.schedule is None
        assert MANIFEST.staleness_probe is None
        assert MANIFEST.provides == ["docs.write"]


class TestCallerAccount:
    """`_caller_account` is the ONE credential lookup every Docs tool makes.

    Decision (Alex, 2026-09-06): a tool never falls back to another user's
    Google token. The previous `_account(prefer_owner=...)` had two lookup
    orders and the other account as the fallback in both; a caller with no
    token of their own acted with the configured owner's reach.
    """

    CALLER = "caller@example.invalid"
    OWNER = "owner@example.invalid"

    def _session(self, *, caller_has_token: bool, owner_configured_with_token: bool = True):
        """OAuthToken lookups answer by filter kwargs: the caller's own row
        (`user_id=`) and — to prove it is never consulted — the configured
        owner's row (`account_email=`), which always exists here."""
        session = MagicMock()
        calls: list[dict] = []

        def query(_model):
            q = MagicMock()

            def filter_by(**kwargs):
                calls.append(kwargs)
                inner = MagicMock()
                if kwargs.get("account_email") == self.OWNER and owner_configured_with_token:
                    inner.first.return_value = MagicMock(account_email=self.OWNER, user_id=7)
                elif "user_id" in kwargs and caller_has_token:
                    inner.first.return_value = MagicMock(
                        account_email=self.CALLER, user_id=kwargs["user_id"],
                    )
                else:
                    inner.first.return_value = None
                return inner

            q.filter_by.side_effect = filter_by
            return q

        session.query.side_effect = query
        session.calls = calls
        return session

    def _resolve(self, session):
        from app.integrations.google_docs.tools import _caller_account

        # docs_owner_account IS configured and DOES have a token — the exact
        # situation in which the old code fell back to it.
        with patch("app.plugin.config_store.plugin_config",
                   return_value=MagicMock(docs_owner_account=self.OWNER)), \
             patch("app.integrations.google_docs.tools.current_user_id", return_value=3):
            return _caller_account(session)

    def test_caller_with_a_token_gets_their_own_account(self):
        session = self._session(caller_has_token=True)
        assert self._resolve(session) == (self.CALLER, 3)

    def test_caller_without_a_token_is_refused_not_given_the_owners(self):
        session = self._session(caller_has_token=False)
        with pytest.raises(PermanentError) as exc:
            self._resolve(session)
        msg = str(exc.value)
        # Names what to do — their own OAuth flow — and never a fallback knob.
        assert "connect your own account" in msg
        assert "/api/auth/google/login" in msg
        assert "docs_owner_account" not in msg

    def test_the_configured_owner_account_is_never_looked_up(self):
        for has_token in (True, False):
            session = self._session(caller_has_token=has_token)
            try:
                self._resolve(session)
            except PermanentError:
                pass
            assert all("account_email" not in c for c in session.calls), session.calls
            assert all(c.get("user_id") == 3 for c in session.calls), session.calls


class TestEveryHandlerUsesTheCallerAccount:
    """Each handler must go through `_caller_account` — and only it.

    Testing `_caller_account` alone is not enough: a handler that kept its
    own owner lookup would pass that test and still act with someone else's
    token. So every handler runs twice: with a caller token (the writer is
    called with exactly that account) and without one (the writer is never
    reached).
    """

    CASES = [
        (handle_read, {"document": "d1"}, "read_markdown"),
        (handle_append, {"document": "d1", "text": "x"}, "append_text"),
        (handle_replace, {"document": "d1", "find": "a"}, "replace_text"),
        (handle_write, {"key": "k", "markdown": "# x"}, "ensure_export"),
    ]

    @pytest.mark.parametrize("handler, arguments, writer_fn", CASES, ids=lambda c: getattr(c, "__name__", str(c)))
    def test_writer_is_called_with_the_callers_account(self, handler, arguments, writer_fn):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.one_or_none.return_value = None
        with patch("app.integrations.google_docs.tools._caller_account",
                   return_value=("caller@example.invalid", 3)), \
             patch("app.integrations.google_docs.tools._share_with", return_value=[]), \
             patch(f"app.integrations.google_docs.writer.{writer_fn}",
                   return_value={} if writer_fn != "ensure_export" else None) as fn:
            handler(session, arguments)
        kwargs = fn.call_args.kwargs
        assert kwargs.get("account_email", kwargs.get("owner_account_email")) == "caller@example.invalid"
        assert kwargs.get("user_id", kwargs.get("owner_user_id")) == 3

    @pytest.mark.parametrize("handler, arguments, writer_fn", CASES, ids=lambda c: getattr(c, "__name__", str(c)))
    def test_no_token_means_no_api_call(self, handler, arguments, writer_fn):
        session = MagicMock()
        session.query.return_value.filter_by.return_value.one_or_none.return_value = None
        with patch("app.integrations.google_docs.tools._caller_account",
                   side_effect=PermanentError("you have no Google account connected")), \
             patch("app.integrations.google_docs.tools._share_with", return_value=[]), \
             patch(f"app.integrations.google_docs.writer.{writer_fn}") as fn, \
             pytest.raises(PermanentError, match="no Google account connected"):
            handler(session, arguments)
        fn.assert_not_called()

    def test_no_handler_reads_docs_owner_account(self):
        """The retired config key must not be reachable from any tool path —
        source-level, so a re-introduced fallback is caught even if it is
        never hit by the mocked calls above."""
        import inspect

        from app.integrations.google_docs import tools

        assert "docs_owner_account" not in inspect.getsource(tools).replace(
            # The docstring that records the decision is allowed to name it.
            inspect.getsource(tools._caller_account), "",
        )


class TestWriteMarkdownCredential:
    def test_write_markdown_uses_the_account_it_is_given_not_the_exports_owner(self):
        from app.integrations.google_docs import writer as w

        export = MagicMock(key="k", document_id="d1", owner_account_email="owner@example.invalid")
        drive = MagicMock()
        with patch("app.integrations.google_docs.writer.get_drive_service", return_value=drive) as get:
            w.write_markdown(MagicMock(), export, account_email="caller@example.invalid",
                             user_id=2, markdown="# x")
        assert get.call_args.args[0] == "caller@example.invalid"
        assert get.call_args.kwargs["user_id"] == 2

    def test_write_markdown_with_no_credentials_raises_rather_than_silently_skipping(self):
        from app.integrations.google_docs import writer as w

        export = MagicMock(key="k", document_id="d1", owner_account_email="owner@example.invalid")
        with patch("app.integrations.google_docs.writer.get_drive_service", return_value=None), \
             pytest.raises(PermanentError, match="no Google credentials"):
            w.write_markdown(MagicMock(), export, account_email="c@example.invalid",
                             user_id=2, markdown="# x")
