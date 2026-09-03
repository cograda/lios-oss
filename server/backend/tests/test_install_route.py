"""Tests for the installer route (sam-live Phase 0).

Two things under test, both unit-tier (no DB):

1. `_render()` output — the rendered bash template itself. Checks the
   Phase-0 hardening: the dev-checkout path bug (a `~/Desktop/Code/...` ->
   `~/Comar`), the git-repo guard, the Syncthing-not-Drive docstring, the
   derived (not hardcoded) Syncthing peer address, the dead `SENTINEL_FILE`
   removal, and the new daemon-split-brain-check installer step.
2. `GET /api/auth/google/login` without `?user=` — `user` is now a required
   query param (F-oauth-default fix), so FastAPI should 422 before the
   handler body (and therefore the DB) is ever touched.

Mirrors the mounted-real-router pattern from test_ui_auth_middleware.py:
exercise the actual production route object, not a re-implementation.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.auth import router as auth_router
from app.routes.install import INSTALL_SCRIPT_TEMPLATE, _render


# ---------------------------------------------------------------------------
# _render() — rendered installer template content
# ---------------------------------------------------------------------------


def _rendered():
    return _render(
        server_url="https://example.tail-scale.ts.net",
        token="tok_abc123",
        user="sam",
        label="sam-macbook",
    )


class TestRenderedTemplate:
    def test_no_dev_checkout_path_survives(self):
        """The dev-machine checkout path must not leak into a script meant for
        a family member's machine — every doc promises `~/Comar/`. Asserted on
        the `Desktop/Code` prefix rather than a named repo, because the repo it
        used to name (`comar`) became `lios/core` on 2026-09-01 and a guard
        keyed to one repo name stops guarding the moment that name changes."""
        script = _rendered()
        assert "Desktop/Code" not in script

    def test_working_and_vault_dirs_point_at_home_comar(self):
        script = _rendered()
        assert 'WORKING_DIR="${HOME}/Comar"' in script
        assert 'VAULT_DIR="${WORKING_DIR}/vault"' in script

    def test_next_steps_point_at_home_comar(self):
        script = _rendered()
        assert "~/Comar" in script
        assert "Desktop/Code" not in script

    def test_git_repo_guard_present_and_runs_before_gates(self):
        script = _rendered()
        assert 'if [[ -d "${WORKING_DIR}/.git" ]]; then' in script
        assert "refusing to install into a git repository" in script

        # Guard must run before the first gate, not after.
        guard_pos = script.index("refusing to install into a git repository")
        gate1_pos = script.index("gate_xcode()")
        assert guard_pos < gate1_pos

    def test_docstring_says_syncthing_not_google_drive(self):
        # The module docstring, not the template — but keep both consistent:
        # neither should still claim Google Drive is what Gate 3 does.
        import app.routes.install as install_module

        assert "Google Drive vault sync" not in install_module.__doc__
        assert "Syncthing" in install_module.__doc__

    def test_sentinel_file_removed(self):
        script = _rendered()
        assert "SENTINEL_FILE" not in script
        assert ".comar-synced" not in script

    def test_syncthing_peer_host_is_derived_not_hardcoded(self):
        script = _rendered()
        assert "ubuntudockerbox" not in script
        assert "tail78010b" not in script
        # The derivation itself must be present.
        assert "sync_host" in script
        assert 'tcp://${sync_host}:22000' in script

    def test_daemon_check_step_present_and_scheduled(self):
        script = _rendered()
        assert "step_daemon_check" in script
        assert "com.lios.sync.daemoncheck" in script
        assert "<key>StartInterval</key><integer>1800</integer>" in script
        # Called in the invocation sequence, not just defined.
        call_site = script.rindex("step_daemon_check")
        definition_site = script.index("step_daemon_check()")
        assert call_site > definition_site

    def test_env_sh_warns_on_unreadable_config(self):
        script = _rendered()
        assert "LIOS_TOKEN not set" in script
        assert "unreadable" in script

    def test_render_substitutes_placeholders(self):
        script = _rendered()
        assert "tok_abc123" in script
        assert "sam" in script
        assert "sam-macbook" in script
        assert "https://example.tail-scale.ts.net" in script
        # No leftover __VAR__ placeholders.
        assert "__SERVER__" not in script
        assert "__TOKEN__" not in script
        assert "__USER__" not in script
        assert "__LABEL__" not in script

    def test_template_itself_has_no_dev_checkout_path(self):
        # Belt-and-braces: check the unrendered template too, not just one
        # rendering of it.
        assert "Desktop/Code" not in INSTALL_SCRIPT_TEMPLATE


# ---------------------------------------------------------------------------
# GET /api/auth/google/login — `user` now required
# ---------------------------------------------------------------------------


def _auth_app() -> FastAPI:
    # `auth_router` already carries its own `prefix="/auth"` (see
    # app/routes/auth.py); mount it the same way app/routes/__init__.py does,
    # under the shared "/api" prefix, so the exercised path matches production.
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    return app


class TestGoogleLoginRequiresUser:
    def test_missing_user_is_422(self):
        client = TestClient(_auth_app())
        resp = client.get("/api/auth/google/login", params={"account": "test@gmail.com"})
        assert resp.status_code == 422

    def test_missing_account_and_user_is_422(self):
        client = TestClient(_auth_app())
        resp = client.get("/api/auth/google/login")
        assert resp.status_code == 422

    def test_signature_has_no_default_for_user(self):
        """Belt-and-braces on the function signature itself: `user` must not
        carry a default value (the whole point of the fix — a forgotten
        `?user=` used to silently attribute the token to "alex")."""
        import inspect

        from app.routes.auth import google_login

        sig = inspect.signature(google_login)
        assert sig.parameters["user"].default is inspect.Parameter.empty
