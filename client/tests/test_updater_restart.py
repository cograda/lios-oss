"""The updater's restart must target the launchd label the installer actually
writes, and must never leave the old process running when it does not.

2026-09-06: the label was still the pre-rename `com.cograda.comar`, so every
auto-update since the 2026-09-02 rename installed the wheel and then carried
on running the old code — the heartbeat said 3.0.0 while the disk said 3.0.1.
"""

from lios_sync import updater
from lios_sync.launchd import LEGACY_LABEL, PLIST_LABEL


def _capture_runs(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))

        class R:
            stdout = "501\n"
            returncode = 0

        return R()

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    return calls


def test_restart_targets_the_current_label_first(monkeypatch):
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(updater, "_exit_for_relaunch", lambda: None)

    updater.restart_daemon()

    kickstarts = [c for c in calls if c[:2] == ["launchctl", "kickstart"]]
    assert kickstarts[0] == ["launchctl", "kickstart", "-k", f"gui/501/{PLIST_LABEL}"]
    assert PLIST_LABEL == "com.lios.sync"  # the label launchd.py installs
    assert kickstarts[1][-1].endswith(LEGACY_LABEL)  # unmigrated Mac fallback


def test_restart_exits_when_no_kickstart_killed_us(monkeypatch):
    """KeepAlive relaunches an exited agent with the new binary — the fallback
    that makes a label mismatch survivable."""
    _capture_runs(monkeypatch)
    exited = []
    monkeypatch.setattr(updater, "_exit_for_relaunch", lambda: exited.append(True))

    updater.restart_daemon()

    assert exited == [True]


# ---------------------------------------------------------------------------
# Which installer
# ---------------------------------------------------------------------------
#
# 2026-09-06: Sam's Mac (venv install, no pipx anywhere) downloaded and
# verified 3.0.2 and then raised FileNotFoundError on `pipx install`. The
# interpreter running the updater is the one to upgrade; ask it, not PATH.

from pathlib import Path


def test_pipx_install_used_only_when_running_from_a_pipx_venv(monkeypatch):
    monkeypatch.setattr(updater.sys, "prefix", "/Users/x/.local/pipx/venvs/lios-sync")
    cmd = updater.install_command(Path("/tmp/lios_sync-3.0.3-py3-none-any.whl"))
    assert cmd[:2] == ["pipx", "install"]
    assert "--force" in cmd


def test_plain_venv_install_uses_its_own_interpreter(monkeypatch):
    monkeypatch.setattr(updater.sys, "prefix", "/Users/n/Library/Application Support/lios/venv")
    monkeypatch.setattr(updater.sys, "executable", "/Users/n/Library/Application Support/lios/venv/bin/python3")
    cmd = updater.install_command(Path("/tmp/lios_sync-3.0.3-py3-none-any.whl"))
    assert cmd[:3] == ["/Users/n/Library/Application Support/lios/venv/bin/python3", "-m", "pip"]
    assert "pipx" not in cmd
    assert cmd[-1].endswith("lios_sync-3.0.3-py3-none-any.whl")
