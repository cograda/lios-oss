"""Repoint the capture Shortcuts at comar, per person, reproducibly.

Two Apple Shortcuts feed comar's inbox from a phone: **Dictator** (record a
voice note) and **Send To Comar** (share-sheet any file). Until 2026-08-29 both
POSTed to a Tines webhook. Tines is retired; they now post to
`https://ingest.comar.ie/api/inbox/ingest`.

**Why a script rather than editing them in the Shortcuts app.** A hand-built
client drifts invisibly, which is the exact failure this whole migration
removed: the Tines story carried its own transcription prompt, its own model
pin and its own proper-noun list, and all three went stale with nothing ever
comparing them to comar's. A Shortcut edited by hand on one phone is the same
shape of problem — and there are two phones. This makes the client a build
artefact: reviewable, reproducible, and identical for both people apart from
their credentials.

**Why it transforms the existing definitions rather than authoring plists from
scratch.** A Shortcut is a plist whose actions are wired together by UUID —
one action's `OutputUUID` is referenced inside the next action's parameters via
`attachmentsByRange`. Rebuilding that graph by hand is easy to get subtly and
silently wrong. The exported originals already have correct wiring, so the
smallest safe change is to rename keys and repoint the request, leaving every
attachment reference untouched.

**Secrets.** The generated `.shortcut` files contain a live Cloudflare Access
client secret and a live comar bearer. This script is committed; **its output
is not** — it writes to a gitignored directory and refuses to run without real
credentials. A shortcut emitted with a blank token would fail at the moment of
capture on a thought that cannot be re-recorded, which is strictly worse than
failing here (same reasoning as `lunchcloud-cli`'s blocklist refusing to run
empty — see the copy-and-verify section in `Code/CLAUDE.md`).

Usage:

    python scripts/build_shortcuts.py --exports <dir> --out <dir>
    python scripts/build_shortcuts.py --verify <dir>

`--exports` holds `dictator.actions.plist` / `send-to-comar.actions.plist`,
dumped from `~/Library/Shortcuts/Shortcuts.sqlite` (`ZSHORTCUTACTIONS.ZDATA`,
keyed by `ZSHORTCUT.Z_PK`).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

# Where credentials live: `deploy/certs/`, gitignored by `deploy/.gitignore`.
#
# Located *relative to this file*, not absolutely. These two lines used to read
# `Path.home() / "Desktop/Code/infra/certs/..."`, and on 2026-09-01 the certs
# moved with the rest of infra's local state into `lios/deploy/certs/` — which
# broke this script silently-ish: both files would simply be absent, and it
# would refuse with "no credentials" while the credentials sat on disk one
# directory away. That is the same fixed-path baggage that killed three CI
# workflows and the release sanitiser in the same move (see the repo CLAUDE.md's
# "When an element lands, ask what reads it from a fixed path"). Resolving from
# `__file__` means the path survives the whole tree being moved or renamed.
_LIOS_ROOT = Path(__file__).resolve().parents[2]  # core/scripts/ -> core/ -> lios/
_CERTS = _LIOS_ROOT / "deploy" / "certs"
DEFAULT_ACCESS_ENV = _CERTS / "cloudflare-access-tokens.env"
DEFAULT_BEARER_ENV = _CERTS / "comar-client-tokens.env"

INGEST_URL = "https://ingest.comar.ie/api/inbox/ingest"

# The Tines-era body keys, and what the comar contract calls them. `audio` and
# `file_name` carry *attachments* (references to the base64 and filename action
# outputs), so they are renamed in place — replacing the value would break the
# wiring. The keys not listed here (`email`, `comar`, `file_size`,
# `file_created`) are simply ignored by `routes/inbox.py`, so they are left
# alone rather than surgically removed: every deletion is a chance to break an
# attachment reference, and an ignored key costs nothing.
KEY_RENAMES = {"audio": "data", "file_name": "filename"}

# `type` is a routing hint (`routes/inbox.py::KNOWN_TYPES`). Unknown values
# fall through to /inbox/incoming/ and are classified server-side by magic
# bytes, so this is a hint and not a contract.
TYPE_BY_SHORTCUT = {"dictator": "audio", "send-to-comar": "file"}

PEOPLE = {
    "alex": {
        "access_id": "CAPTURE_ALEX_IPHONE_CLIENT_ID",
        "access_secret": "CAPTURE_ALEX_IPHONE_CLIENT_SECRET",
        "bearer": "DICTATOR_ALEX_TOKEN",
    },
    "sam": {
        "access_id": "CAPTURE_SAM_IPHONE_CLIENT_ID",
        "access_secret": "CAPTURE_SAM_IPHONE_CLIENT_SECRET",
        "bearer": "DICTATOR_SAM_TOKEN",
    },
}


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=value file. Absent file is not fatal here — the credential
    check below is where a missing value becomes a hard error, so that the
    message names the key rather than the file."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _string_item(key: str, value: str) -> dict:
    """A literal key/value pair in a Shortcuts dictionary parameter."""
    return {
        "WFItemType": 0,
        "WFKey": {"Value": {"string": key}, "WFSerializationType": "WFTextTokenString"},
        "WFValue": {"Value": {"string": value}, "WFSerializationType": "WFTextTokenString"},
    }


def _dict_param(items: list[dict]) -> dict:
    return {
        "Value": {"WFDictionaryFieldValueItems": items},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def patch_actions(actions: list, *, kind: str, access_id: str, access_secret: str, bearer: str) -> list:
    """Repoint the one `downloadurl` action at comar. Everything else is left
    exactly as the original had it."""
    found = False
    for action in actions:
        if not action.get("WFWorkflowActionIdentifier", "").endswith("downloadurl"):
            continue
        found = True
        params = action.setdefault("WFWorkflowActionParameters", {})

        params["WFURL"] = INGEST_URL
        params["WFHTTPMethod"] = "POST"
        params["WFHTTPBodyType"] = "JSON"

        params["WFHTTPHeaders"] = _dict_param([
            _string_item("CF-Access-Client-Id", access_id),
            _string_item("CF-Access-Client-Secret", access_secret),
            _string_item("Authorization", f"Bearer {bearer}"),
        ])

        items = params["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
        for item in items:
            key = item["WFKey"]["Value"]["string"]
            if key in KEY_RENAMES:
                item["WFKey"]["Value"]["string"] = KEY_RENAMES[key]

        present = {i["WFKey"]["Value"]["string"] for i in items}
        if "type" not in present:
            items.append(_string_item("type", TYPE_BY_SHORTCUT[kind]))

        # Deliberately NOT set: `metadata.note`. `routes/inbox.py` honours it
        # and it takes precedence over the transcript, so a client that fills
        # it in permanently suppresses the real transcript for that file.

    if not found:
        raise SystemExit(f"{kind}: no downloadurl action found — wrong export?")
    return actions


def build(exports: Path, out: Path, creds: dict[str, str]) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for person, keys in PEOPLE.items():
        missing = [k for k in keys.values() if not creds.get(k)]
        if missing:
            raise SystemExit(
                f"refusing to build {person}: missing credential(s) {missing}. "
                "A shortcut with a blank token fails at the moment of capture, "
                "on a thought that cannot be re-recorded."
            )
        for kind in TYPE_BY_SHORTCUT:
            src = exports / f"{kind}.actions.plist"
            if not src.is_file():
                raise SystemExit(f"missing export: {src}")
            actions = plistlib.loads(src.read_bytes())
            actions = patch_actions(
                actions,
                kind=kind,
                access_id=creds[keys["access_id"]],
                access_secret=creds[keys["access_secret"]],
                bearer=creds[keys["bearer"]],
            )
            dest = out / f"{kind}-{person}.shortcut"
            dest.write_bytes(plistlib.dumps({"WFWorkflowActions": actions}, fmt=plistlib.FMT_BINARY))
            dest.chmod(0o600)
            written.append(dest)
    return written


def verify(paths: list[Path]) -> int:
    """Prove what was built. Must be able to fail — see `Code/CLAUDE.md`."""
    bad = 0
    for p in paths:
        raw = p.read_bytes()
        pl = plistlib.loads(raw)
        actions = pl["WFWorkflowActions"] if isinstance(pl, dict) else pl
        problems = []

        if b"cog.tines.com" in raw:
            problems.append("STILL CONTAINS cog.tines.com")

        for a in actions:
            if not a.get("WFWorkflowActionIdentifier", "").endswith("downloadurl"):
                continue
            params = a["WFWorkflowActionParameters"]
            if params.get("WFURL") != INGEST_URL:
                problems.append(f"url is {params.get('WFURL')!r}")
            hdrs = {
                i["WFKey"]["Value"]["string"]: i["WFValue"]["Value"]["string"]
                for i in params.get("WFHTTPHeaders", {}).get("Value", {}).get("WFDictionaryFieldValueItems", [])
            }
            for h in ("CF-Access-Client-Id", "CF-Access-Client-Secret", "Authorization"):
                if not hdrs.get(h):
                    problems.append(f"header {h} missing/empty")
            if hdrs.get("Authorization") == "Bearer ":
                problems.append("bearer is blank")
            body = {
                i["WFKey"]["Value"]["string"]
                for i in params["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
            }
            for k in ("data", "filename", "type"):
                if k not in body:
                    problems.append(f"body key {k!r} missing")

        status = "FAIL" if problems else "ok"
        print(f"  [{status}] {p.name}")
        for prob in problems:
            print(f"          - {prob}")
        bad += bool(problems)
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exports", type=Path, required=False, help="dir of *.actions.plist exports")
    ap.add_argument("--out", type=Path, required=False, help="output dir (must be gitignored)")
    ap.add_argument("--verify", type=Path, help="verify an existing output dir and exit")
    ap.add_argument("--sign", action="store_true", help="also sign with /usr/bin/shortcuts")
    args = ap.parse_args()

    if args.verify:
        paths = sorted(args.verify.glob("*.shortcut"))
        if not paths:
            print(f"no .shortcut files in {args.verify}")
            return 1
        print(f"verifying {len(paths)} shortcut(s):")
        return 1 if verify(paths) else 0

    if not args.exports or not args.out:
        ap.error("--exports and --out are required unless --verify is given")

    creds = {**read_env_file(DEFAULT_ACCESS_ENV), **read_env_file(DEFAULT_BEARER_ENV), **os.environ}
    written = build(args.exports, args.out, creds)

    if args.sign:
        for p in written:
            signed = p.with_suffix(".signed.shortcut")
            r = subprocess.run(
                ["/usr/bin/shortcuts", "sign", "-i", str(p), "-o", str(signed), "-m", "anyone"],
                capture_output=True, text=True,
            )
            print(f"  sign {p.name}: {'ok' if r.returncode == 0 else r.stderr.strip()[:120]}")

    print(f"\nwrote {len(written)} shortcut(s) to {args.out}")
    print("verifying:")
    return 1 if verify(written) else 0


if __name__ == "__main__":
    sys.exit(main())
