#!/usr/bin/env python3
"""Apple Reminders sync agent for macOS.

Reads reminders via osascript (AppleScript bulk property access),
pushes state to the comar server API, and executes queued commands.

Runs via launchd every 5 minutes.

Usage:
    python3 sync.py                 # one-shot sync
    python3 sync.py --daemon 300    # loop every 300 seconds
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("reminders-sync")

# Config — override with environment variables
SERVER_URL = os.environ.get("REMINDERS_SERVER_URL", "http://localhost:8400")
AUTH_TOKEN = os.environ.get("REMINDERS_AUTH_TOKEN", "")

HEADERS = {"Content-Type": "application/json"}
if AUTH_TOKEN:
    HEADERS["Authorization"] = f"Bearer {AUTH_TOKEN}"


# ---------------------------------------------------------------------------
# Read reminders via AppleScript (bulk property access per list)
# ---------------------------------------------------------------------------


def _osa(script: str, timeout: int = 10) -> str | None:
    """Run an AppleScript and return stdout, or None on failure."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            logger.debug(f"osascript error: {r.stderr.strip()}")
            return None
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning(f"osascript timeout: {script[:80]}...")
        return None


def _split_osa_list(raw: str) -> list[str]:
    """Split an AppleScript comma-separated list, handling edge cases."""
    if not raw:
        return []
    # Single item has no comma
    if ", " not in raw:
        return [raw]
    return [item.strip() for item in raw.split(", ")]


def read_reminders() -> list[dict]:
    """Read all incomplete reminders from Apple Reminders."""
    raw_lists = _osa('tell application "Reminders" to name of every list')
    if not raw_lists:
        logger.error("Could not read reminder lists")
        return []

    list_names = _split_osa_list(raw_lists)
    results = []

    for ln in list_names:
        escaped = ln.replace('"', '\\"')
        base = f'tell application "Reminders" to {{prop}} of every reminder of list "{escaped}" whose completed is false'

        names = _osa(base.format(prop="name"))
        if not names:
            continue

        ids = _osa(base.format(prop="id"))
        prios = _osa(base.format(prop="priority"))

        if not ids:
            continue

        name_list = _split_osa_list(names)
        id_list = _split_osa_list(ids)
        prio_list = [int(p) for p in _split_osa_list(prios)] if prios else [0] * len(name_list)

        # Due dates need special handling — AppleScript returns "missing value" for unset
        due_dates_raw = _osa(base.format(prop="due date"))
        due_list = _split_osa_list(due_dates_raw) if due_dates_raw else []

        for i in range(min(len(name_list), len(id_list))):
            due_date = None
            if i < len(due_list) and due_list[i] != "missing value":
                # AppleScript dates are locale-dependent; parse as best we can
                due_date = _parse_osa_date(due_list[i])

            results.append({
                "uid": id_list[i],
                "list_name": ln,
                "summary": name_list[i],
                "priority": prio_list[i] if i < len(prio_list) else 0,
                "completed": False,
                "due_date": due_date,
            })

        logger.info(f"  {ln}: {len(name_list)} reminders")

    return results


def _parse_osa_date(raw: str) -> str | None:
    """Parse an AppleScript date string into ISO format.

    AppleScript dates look like: "Saturday 5 April 2026 at 09:00:00"
    Format varies by locale.
    """
    from datetime import datetime
    formats = [
        "%A %d %B %Y at %H:%M:%S",  # Saturday 5 April 2026 at 09:00:00
        "%A, %d %B %Y at %H:%M:%S",  # Saturday, 5 April 2026 at 09:00:00
        "%d %B %Y at %H:%M:%S",  # 5 April 2026 at 09:00:00
        "%A %B %d, %Y at %I:%M:%S %p",  # US locale
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.isoformat()
        except ValueError:
            continue
    logger.debug(f"Could not parse date: {raw}")
    return None


# ---------------------------------------------------------------------------
# Push to server
# ---------------------------------------------------------------------------


def push_reminders(reminders: list[dict]) -> bool:
    """Push reminder state to the comar server API."""
    url = f"{SERVER_URL}/api/reminders/sync"
    try:
        resp = requests.post(url, json={"reminders": reminders}, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"Pushed {data.get('synced', 0)} reminders to server")
            return True
        else:
            logger.error(f"Push failed: {resp.status_code} {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        logger.error(f"Push failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Execute queued commands
# ---------------------------------------------------------------------------


def _build_add_script(payload: dict) -> str:
    """Build AppleScript to add a reminder."""
    summary = payload.get("summary", "").replace('"', '\\"')
    list_name = payload.get("list", "Reminders").replace('"', '\\"')
    notes = (payload.get("notes") or "").replace('"', '\\"')

    script = f'''
tell application "Reminders"
    set targetList to list "{list_name}"
    set newReminder to make new reminder at end of reminders of targetList
    set name of newReminder to "{summary}"
'''
    if notes:
        script += f'    set body of newReminder to "{notes}"\n'

    if payload.get("due_date"):
        # Convert ISO date to AppleScript date
        script += f'    set due date of newReminder to date "{payload["due_date"]}"\n'

    prio_map = {"high": 1, "medium": 5, "low": 9, "none": 0}
    prio = prio_map.get(payload.get("priority", "none"), 0)
    if prio:
        script += f"    set priority of newReminder to {prio}\n"

    script += """end tell
"done"
"""
    return script


def _build_complete_script(payload: dict) -> str:
    """Build AppleScript to complete a reminder by name."""
    summary = payload.get("summary", "").replace('"', '\\"')
    list_name = payload.get("list", "Reminders").replace('"', '\\"')

    return f'''
tell application "Reminders"
    set targetReminders to every reminder of list "{list_name}" whose name is "{summary}" and completed is false
    repeat with r in targetReminders
        set completed of r to true
    end repeat
end tell
"done"
'''


def execute_command(cmd: dict) -> bool:
    """Execute a single command via osascript."""
    action = cmd["action"]
    payload = cmd["payload"]

    if action == "add":
        script = _build_add_script(payload)
    elif action == "complete":
        script = _build_complete_script(payload)
    else:
        logger.warning(f"Unknown command action: {action}")
        return False

    result = _osa(script, timeout=15)
    if result is not None:
        logger.info(f"Executed {action}: {payload.get('summary', '')}")
        return True
    else:
        logger.error(f"Failed to execute {action}: {payload.get('summary', '')}")
        return False


def fetch_and_execute_commands():
    """Fetch pending commands from server and execute them."""
    url = f"{SERVER_URL}/api/reminders/commands"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch commands: {resp.status_code}")
            return

        commands = resp.json().get("commands", [])
        if not commands:
            return

        logger.info(f"Processing {len(commands)} commands")
        for cmd in commands:
            execute_command(cmd)
            # Mark as done on server
            mark_url = f"{SERVER_URL}/api/reminders/commands/{cmd['id']}/done"
            try:
                requests.post(mark_url, headers=HEADERS, timeout=10)
            except requests.RequestException:
                pass

    except requests.RequestException as e:
        logger.error(f"Failed to fetch commands: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def sync_once():
    """Run one full sync cycle: execute commands → read → push."""
    logger.info("Starting sync cycle")

    # 1. Execute queued commands first (add/complete happen before we read,
    #    so the push will include the result)
    fetch_and_execute_commands()
    time.sleep(1)  # Let Reminders process changes

    # 2. Read current reminders
    reminders = read_reminders()
    logger.info(f"Read {len(reminders)} incomplete reminders")

    # 3. Push to server
    push_reminders(reminders)

    logger.info("Sync cycle complete")


def main():
    parser = argparse.ArgumentParser(description="Apple Reminders sync agent")
    parser.add_argument(
        "--daemon", type=int, metavar="SECONDS",
        help="Run continuously, syncing every N seconds",
    )
    args = parser.parse_args()

    if args.daemon:
        logger.info(f"Daemon mode: syncing every {args.daemon}s")
        while True:
            try:
                sync_once()
            except Exception:
                logger.exception("Sync cycle failed")
            time.sleep(args.daemon)
    else:
        sync_once()


if __name__ == "__main__":
    main()
