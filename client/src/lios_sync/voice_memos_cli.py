"""`lios-sync voice-memos` — inspect and backfill voice-memo capture.

Kept apart from `voice_memos.py` so the watcher module stays import-light for the
daemon: nothing here is loaded unless the command is actually run.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lios_sync.config import load_config
from lios_sync.server_client import ServerClient
from lios_sync.voice_memos import (
    AUDIO_SUFFIXES,
    LEDGER_PATH,
    MAX_BYTES,
    MIN_BYTES,
    RECORDINGS_DIR,
    UploadLedger,
    VoiceMemoHandler,
    _fingerprint,
)


def _recordings_dir(config) -> Path:
    return config.voice_memos.recordings_path or RECORDINGS_DIR


def _classify(path: Path, ledger: UploadLedger) -> str:
    """Why a given recording would or wouldn't be uploaded."""
    size = path.stat().st_size
    if size < MIN_BYTES:
        return "skip: below the mis-tap floor"
    if size > MAX_BYTES:
        return "skip: over the upload cap"
    try:
        if _fingerprint(path) in ledger:
            return "skip: already uploaded"
    except OSError as exc:
        return f"skip: unreadable ({exc})"
    return "would upload"


def run_voice_memos(*, backfill: bool = False, limit: int | None = None,
                    dry_run: bool = False) -> None:
    config = load_config()
    recordings = _recordings_dir(config)

    if not config.voice_memos.enabled:
        print("Voice-memo capture is disabled.")
        print("Enable it in ~/.config/lios/config.toml:\n")
        print("    [voice_memos]\n    enabled = true\n")
        print("Then restart the daemon: launchctl kickstart -k gui/$(id -u)/com.lios.sync")
        # Not an error exit: reporting the disabled state is a valid answer to
        # `lios-sync voice-memos`, and scripts shouldn't treat it as a failure.

    if not recordings.is_dir():
        print(f"Voice Memos container not found: {recordings}")
        sys.exit(1)

    try:
        files = sorted(p for p in recordings.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES)
    except PermissionError:
        print(f"No permission to read {recordings}")
        print(
            "Grant Full Disk Access to your terminal (and to the daemon) in\n"
            "System Settings › Privacy & Security › Full Disk Access, then retry."
        )
        sys.exit(1)

    try:
        ledger = UploadLedger()
    except Exception:
        # UploadLedger raises rather than silently re-uploading everything, since
        # a lost ledger means paying for every transcription again.
        print("The upload ledger is unreadable — see the logged traceback.")
        print(f"Fix or delete {LEDGER_PATH}, then retry.")
        sys.exit(1)

    print(f"Recordings dir : {recordings}")
    print(f"Recordings     : {len(files)}")
    print(f"Already sent   : {ledger.count}")

    if not backfill:
        pending = [p for p in files if _classify(p, ledger) == "would upload"]
        print(f"Not yet sent   : {len(pending)}")
        if pending:
            print("\nRun with --backfill to upload them "
                  "(--dry-run first to see the list, --limit N to go slowly).")
        return

    if dry_run:
        print("\nDry run — nothing will be uploaded.\n")
        shown = 0
        for path in files:
            verdict = _classify(path, ledger)
            if verdict != "would upload":
                continue
            shown += 1
            if limit is not None and shown > limit:
                print(f"... (--limit {limit} reached; more remain)")
                break
            size_kb = path.stat().st_size / 1024
            print(f"  {path.name}  ({size_kb:.0f} KB)  {verdict}")
        if shown == 0:
            print("  nothing pending")
        return

    server_client = ServerClient(config)
    handler = VoiceMemoHandler(server_client, ledger)
    print("\nUploading — the server transcribes on its own schedule "
          "(recordings that already carry an Apple transcript cost nothing).\n")
    counts = handler.scan_existing(limit=limit)
    print(
        f"\nconsidered={counts['considered']} "
        f"uploaded={counts['uploaded']} skipped={counts['skipped']}"
    )
    if handler.failed:
        print(f"failed={handler.failed} — these were NOT ledgered and will retry")
