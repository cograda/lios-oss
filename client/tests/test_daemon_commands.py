"""Tests for the daemon's command self-refresh (sam-live Phase 0, task 0.3).

Drives `_refresh_commands` directly with a stub ServerClient and a tmp_path
standing in for `~/Comar/` — no network, no real filesystem outside pytest's
tmp dir.
"""

from types import SimpleNamespace

from lios_sync.daemon import _refresh_commands


class StubServerClient:
    def __init__(self, payload=None, raises=None):
        self.payload = payload or {"claude_md": "", "commands": {}}
        self.raises = raises
        self.calls = 0

    def get_commands(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.payload


def _config(vault_path):
    """Minimal stand-in for ClientConfig — _refresh_commands only reads
    `config.vault.path`."""
    return SimpleNamespace(vault=SimpleNamespace(path=vault_path))


def test_skips_when_working_dir_is_a_dev_repo(tmp_path):
    """vault.path.parent holding a .git means this is the developer's Mac,
    where CLAUDE.md is a hand-written file that must never be clobbered."""
    working_dir = tmp_path / "comar"
    (working_dir / ".git").mkdir(parents=True)
    (working_dir / ".claude" / "commands").mkdir(parents=True)
    vault_path = working_dir / "vault"
    vault_path.mkdir()

    client = StubServerClient(payload={"claude_md": "# should not land", "commands": {}})
    _refresh_commands(client, _config(vault_path))

    assert client.calls == 0
    assert not (working_dir / "CLAUDE.md").exists()


def test_skips_when_no_provisioned_commands_dir(tmp_path):
    """Only refresh a folder the installer actually provisioned."""
    working_dir = tmp_path / "Comar"
    working_dir.mkdir()
    vault_path = working_dir / "vault"
    vault_path.mkdir()
    # No .claude/commands directory at all.

    client = StubServerClient(payload={"claude_md": "# should not land", "commands": {}})
    _refresh_commands(client, _config(vault_path))

    assert client.calls == 0
    assert not (working_dir / "CLAUDE.md").exists()


def test_writes_commands_and_claude_md_and_prunes_stale(tmp_path):
    working_dir = tmp_path / "Comar"
    commands_dir = working_dir / ".claude" / "commands"
    commands_dir.mkdir(parents=True)
    vault_path = working_dir / "vault"
    vault_path.mkdir()

    # A command the server no longer returns — must be pruned.
    stale = commands_dir / "retired.md"
    stale.write_text("old content", encoding="utf-8")

    client = StubServerClient(payload={
        "claude_md": "# Comar\n\nhello",
        "commands": {
            "daily-note.md": "daily note body",
            "meeting.md": "meeting body",
        },
    })

    _refresh_commands(client, _config(vault_path))

    assert client.calls == 1
    assert (working_dir / "CLAUDE.md").read_text(encoding="utf-8") == "# Comar\n\nhello"
    assert (commands_dir / "daily-note.md").read_text(encoding="utf-8") == "daily note body"
    assert (commands_dir / "meeting.md").read_text(encoding="utf-8") == "meeting body"
    assert not stale.exists()


def test_never_raises_on_server_failure(tmp_path):
    working_dir = tmp_path / "Comar"
    commands_dir = working_dir / ".claude" / "commands"
    commands_dir.mkdir(parents=True)
    vault_path = working_dir / "vault"
    vault_path.mkdir()

    client = StubServerClient(raises=ConnectionError("server unreachable"))

    # Must not raise — this is a convenience refresh, never fatal.
    _refresh_commands(client, _config(vault_path))

    assert not (working_dir / "CLAUDE.md").exists()
