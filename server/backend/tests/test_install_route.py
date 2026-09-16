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

import pathlib

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
    def test_no_dev_checkout_path_assigned_as_working_dir(self):
        """The dev-machine checkout path must never become WORKING_DIR — every
        doc promises `~/Comar/` or `~/lios/`. `Desktop/Code` is now
        deliberately present elsewhere in the script (the clean-slate residue
        detector needs to name the old path to retire it), so the precise
        assertion is that it's never assigned, not that the substring never
        appears at all — see TestCleanSlate below for the residue-detector
        coverage of that same string."""
        script = _rendered()
        assert 'WORKING_DIR="${HOME}/Desktop/Code' not in script

    def test_working_and_vault_dirs_point_at_home_comar(self):
        script = _rendered()
        assert 'WORKING_DIR="${HOME}/Comar"' in script
        assert 'VAULT_DIR="${WORKING_DIR}/vault"' in script

    def test_next_steps_point_at_home_comar(self):
        script = _rendered()
        assert "~/Comar" in script or "~/lios" in script

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

    def test_template_itself_never_assigns_dev_checkout_path(self):
        # Belt-and-braces: check the unrendered template too, not just one
        # rendering of it.
        assert 'WORKING_DIR="${HOME}/Desktop/Code' not in INSTALL_SCRIPT_TEMPLATE


# ---------------------------------------------------------------------------
# Clean-slate mode (sam-installer-from-scratch) — residue detection and
# retirement of pre-2026-09-02 "comar" state.
# ---------------------------------------------------------------------------


class TestCleanSlate:
    def test_old_launchd_labels_named_for_retirement(self):
        script = _rendered()
        assert "com.cograda.comar" in script
        assert "com.cograda.comar.syncthing" in script
        assert "com.cograda.comar.daemoncheck" in script
        # Must actually be booted out and unloaded, not just mentioned.
        assert "launchctl bootout" in script

    def test_old_pipx_package_uninstalled(self):
        script = _rendered()
        assert "pipx uninstall comar" in script

    def test_old_working_dir_candidates_named_and_renamed_not_deleted(self):
        script = _rendered()
        assert "Desktop/Code/comar" in script
        assert "pre-lios-" in script
        # Never deleted outright.
        assert 'rm -rf "${old_dir}"' not in script
        assert "mv " in script

    def test_old_config_dir_removed_with_nothing_copied_first(self):
        script = _rendered()
        assert 'OLD_CONFIG_DIR="${HOME}/.config/comar"' in script
        assert 'rm -rf "${OLD_CONFIG_DIR}"' in script

    def test_gate_clean_slate_runs_before_git_repo_guard(self):
        script = _rendered()
        clean_slate_pos = script.index("gate_clean_slate\n")
        guard_pos = script.index("refusing to install into a git repository")
        assert clean_slate_pos < guard_pos

    def test_clean_slate_prompt_gated_by_env_var(self):
        script = _rendered()
        assert "LIOS_CLEAN_SLATE" in script

    def test_clean_slate_idempotent_when_no_residue(self):
        script = _rendered()
        assert "No pre-lios residue found" in script

    def test_permissions_check_step_present_and_scheduled_after_launchd(self):
        script = _rendered()
        assert "step_permissions_check" in script
        assert "Reminders" in script
        launchd_call = script.index("step_launchd\n")
        perm_call = script.index("step_permissions_check\n")
        assert launchd_call < perm_call

    def test_tailscale_detected_via_app_bundle_not_just_path(self):
        """The Tailscale Mac app installs no `tailscale` on PATH; the CLI is
        inside the bundle. 2026-09-06: the PATH-only check made the installer
        try to download a package on a Mac that already had Tailscale running
        (and the URL it used was a 404). The check must fall back to the
        bundle, and every status call must go through the resolved binary."""
        script = _rendered()
        assert '/Applications/Tailscale.app/Contents/MacOS/Tailscale' in script
        assert '"${TAILSCALE_BIN}" status --json' in script
        # No line may still invoke a bare `tailscale` and hope it is on PATH.
        bare = [l for l in script.splitlines()
                if l.strip().startswith("tailscale ") or "$(tailscale " in l]
        assert bare == [], bare
        assert "if ! tailscale_installed; then" in script
        assert "if ! command -v tailscale" not in script

    def test_tailscale_pkg_url_is_the_macos_one(self):
        script = _rendered()
        assert "https://pkgs.tailscale.com/stable/Tailscale-latest-macos.pkg" in script
        assert "stable/Tailscale-latest.pkg" not in script

    def test_claude_code_gate_accepts_desktop_app_and_cli(self):
        """Only one of Claude Code's three Mac forms is a bundle named
        "Claude Code.app". A gate that waits on that one name never returns
        on a Mac with Claude.app or the `claude` CLI (wait_until has no
        timeout by design)."""
        script = _rendered()
        assert 'claude_code_present() {' in script
        assert '[[ -d "/Applications/Claude.app" ]] && return 0' in script
        assert 'command -v claude >/dev/null 2>&1' in script
        assert 'wait_until "Claude Code installed" claude_code_present' in script
        assert 'test -d "/Applications/Claude Code.app"' not in script

    def test_rendered_script_is_valid_bash(self):
        import shutil, subprocess
        if not shutil.which("bash"):  # pragma: no cover
            return
        r = subprocess.run(["bash", "-n"], input=_rendered(), text=True, capture_output=True)
        assert r.returncode == 0, r.stderr

    def test_syncthing_downloads_the_zip_asset_that_actually_exists(self):
        """Syncthing's macOS release assets are .zip (universal build available);
        the tar.gz names used until 2026-09-06 never existed on GitHub, so the
        gate 404'd on every Mac without a pre-existing Syncthing."""
        script = _rendered()
        assert 'zipname="syncthing-macos-universal-${SYNCTHING_VERSION}.zip"' in script
        assert 'unzip -q "${tmp}/syncthing.zip" -d "${tmp}"' in script
        assert ".tar.gz" not in script
        assert "tar xzf" not in script
        import re
        m = re.search(r'^SYNCTHING_VERSION="(v\d+\.\d+\.\d+)"$', script, re.M)
        assert m, "SYNCTHING_VERSION must be a pinned vX.Y.Z"
        assert int(m.group(1)[1:].split(".")[0]) >= 2

    def test_script_tells_the_user_how_to_rerun_without_a_code(self):
        script = _rendered()
        assert "/api/install/rerun | bash" in script

    def test_no_bash4_only_syntax(self):
        """macOS ships bash 3.2 and a fresh Mac runs the script under it.
        ${USER_NAME^} was a 'bad substitution' that killed the 2026-09-06 run
        mid-pairing; Alex's Mac hid it because Homebrew's bash 5 is on PATH."""
        import re
        script = _rendered()
        bash4 = re.compile(
            r"\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^?|,,?)[}]"   # ${v^} ${v^^} ${v,} ${v,,}
            r"|\bdeclare -A\b|\bmapfile\b|\breadarray\b|\|&|&>>|;;&|\[\[ -v "
        )
        hits = [l for l in script.splitlines() if not l.lstrip().startswith("#") and bash4.search(l)]
        assert hits == [], hits
        assert 'USER_LABEL="$(printf' in script
        assert "${USER_LABEL} Vault" in script

    def test_rendered_script_parses_under_stock_macos_bash(self):
        import shutil, subprocess
        if not (shutil.which("bash") and pathlib.Path("/bin/bash").exists()):  # pragma: no cover
            return
        r = subprocess.run(["/bin/bash", "-n"], input=_rendered(), text=True, capture_output=True)
        assert r.returncode == 0, r.stderr

    def test_daemon_venv_requires_python_311_and_can_bootstrap_one(self):
        """Apple's CLT python3 is 3.9; the wheel needs >= 3.11 (client/pyproject).
        The old step built a 3.9 venv and pip then refused the wheel."""
        script = _rendered()
        assert 'PY_MIN="3.11"' in script
        assert "find_python() {" in script
        assert 'py="$(find_python)" || die' in script
        assert '"${py}" -m venv "${venv}"' in script
        assert "    python3 -m venv" not in script
        assert "https://astral.sh/uv/install.sh" in script
        assert 'python install 3.12' in script
        # Version gate is a real comparison, not a string match on "3.9".
        assert "sys.version_info >= (${PY_MIN//./, })" in script

    def test_env_shim_exports_the_variable_mcp_json_reads(self):
        """.mcp.json is rendered with ${LIOS_TOKEN}. The shim assigned LIOS_TOKEN
        and then exported COMAR_TOKEN (never assigned), so GUI-launched Claude
        Code got neither."""
        script = _rendered()
        assert 'Bearer \\${LIOS_TOKEN}' in script or "Bearer ${LIOS_TOKEN}" in script
        assert "export LIOS_TOKEN COMAR_TOKEN" in script
        assert 'COMAR_TOKEN="${LIOS_TOKEN}"' in script
        assert "\n    export COMAR_TOKEN\n" not in script

    def test_wheel_is_saved_under_its_real_filename(self):
        """pip validates name-version-tags.whl before reading the file;
        saving as lios_sync.whl was 'Invalid wheel filename'."""
        script = _rendered()
        assert 'curl -fsSL -OJ -H "Authorization: Bearer ${TOKEN}" "${SERVER}/api/client/download/latest"' in script
        code = [l for l in script.splitlines() if not l.lstrip().startswith('#')]
        assert not any('lios_sync.whl' in l for l in code)
        assert 'install --quiet --force-reinstall "${whl}"' in script

    def test_no_heredoc_program_also_reads_stdin(self):
        """`echo x | python3 - <<'EOF'` gives stdin to the heredoc, so the
        program's json.load(sys.stdin) reads EOF. The commands step did exactly
        this and had never succeeded."""
        import re
        script = _rendered()
        bad = [l for l in script.splitlines() if not l.lstrip().startswith("#") and re.search(r"\|\s*python3\s+-?\s.*<<", l) or re.search(r"\|\s*python3\s*<<", l)]
        assert bad == [], bad
        assert 'python3 - "${proj}" "${resp_file}" <<' in script
        assert 'data = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))' in script

    def test_launchd_runs_the_daemon_subcommand_that_exists(self):
        """The CLI's long-running entry point is `lios-sync daemon`; the plist
        said `run`, which does not exist, so launchd crash-looped it (exit 2)
        on 2026-09-06 while `lios-sync status` still reported the agent loaded."""
        script = _rendered()
        assert "<string>${comar_bin}</string><string>daemon</string>" in script
        assert "<string>${comar_bin}</string><string>run</string>" not in script

    def test_sync_check_stops_on_folder_error_instead_of_reporting_in_sync(self):
        """needFiles=0 is also what an errored (non-scanning) folder reports.
        'folder marker missing' passed as 'in sync' with an empty vault."""
        script = _rendered()
        assert 'if [[ "${state}" == "error" || "${err}" != "-" ]]; then' in script
        assert 'die "Syncthing folder ${personal_folder} is in error' in script
        assert '"${need}" == "0" && "${state}" == "idle"' in script
        assert '"${loc}" == "${glob}"' not in script  # ignored files make local < global
        assert 'stfolder' not in "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#") and "mkdir" in l)

    def test_health_probes_cannot_kill_the_script_under_set_e(self):
        """A refused connection in `x="$(curl …)"` is a failed assignment,
        and set -e exits on it. The Reminders wait probed the daemon one
        second after launchd restarted it; every run ended there, silently."""
        import re
        script = _rendered()
        assert "set -e" in script.splitlines()[1] or re.search(r"^set -[a-z]*e", script, re.M)
        joined = re.sub(r"\\\n\s*", " ", script)  # fold line continuations
        bad = [l for l in joined.splitlines()
               if re.match(r'^\s*[a-z_]+="?\$\(curl ', l) and "||" not in l]
        assert bad == [], bad

    def test_clean_slate_retires_old_env_shim_agent(self):
        script = _rendered()
        assert '"com.cograda.comar.env"' in script

    def test_full_disk_access_not_required_for_base_install(self):
        # Reminders needs granting; Full Disk Access is documented as NOT
        # needed for this install (voice memos are opt-in and off by default,
        # and WORKING_DIR is ~/lios, outside the FDA-protected folders).
        script = _rendered()
        assert "NOT required for this install" in script or "not required for this install" in script.lower()


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


# ---------------------------------------------------------------------------
# GET /api/install/rerun — bearer-authenticated re-render, no code needed
# ---------------------------------------------------------------------------

from app.auth.client_token import get_current_user
from app.routes.install import router as install_router


def _install_app() -> FastAPI:
    app = FastAPI()
    app.include_router(install_router, prefix="/api")
    return app


class TestRerunRoute:
    def test_rerun_without_bearer_is_401_not_treated_as_a_code(self):
        """Declared before /{code}: an unauthenticated GET must be a 401 from
        the bearer dependency, not a 404 'Unknown install code' (which would
        also spend the caller's install-code failure budget)."""
        resp = TestClient(_install_app()).get("/api/install/rerun")
        assert resp.status_code == 401

    def test_rerun_renders_the_script_for_the_bearer_owner(self, monkeypatch):
        import types
        from app.routes import install as install_mod

        class _Sess:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *_a, **_k):
                return types.SimpleNamespace(scalar_one_or_none=lambda: None)

        monkeypatch.setattr(install_mod, "get_db", lambda: types.SimpleNamespace(session=lambda: _Sess()))
        app = _install_app()
        app.dependency_overrides[get_current_user] = lambda: types.SimpleNamespace(name="sam", id=2)
        resp = TestClient(app).get(
            "/api/install/rerun", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/x-shellscript")
        body = resp.text
        assert 'USER_NAME="sam"' in body
        assert 'TOKEN="not-a-real-token"' in body
        assert "__TOKEN__" not in body and "__USER__" not in body
