"""lios-sync CLI — entry point for all client operations.

Usage:
    lios-sync setup           Interactive first-time setup
    lios-sync daemon          Start the daemon (side-car: EventKit + vault watcher)
    lios-sync status          Show daemon and connection status
    lios-sync install         Install/update the launchd agent
    lios-sync uninstall       Remove the launchd agent
    lios-sync import-health   Import Apple Health data from a JSON file
    lios-sync voice-memos     Inspect or backfill Apple Voice Memos capture
"""

import click


@click.group()
@click.version_option()
def main():
    """lios-sync — the local side-car that syncs this Mac into lios.

    Formerly `comar` (Co-Managed Archive), renamed 2026-09-02.
    """
    from lios_sync.config import migrate_legacy_config

    if migrate_legacy_config():
        click.echo("  Copied ~/.config/comar → ~/.config/lios (first run under the new name)")


@main.command()
@click.option("--token", help="Server auth token (skips interactive mode)")
@click.option("--user", help="Username (alex or sam)")
@click.option(
    "--server",
    help=(
        "Server base URL, or a comma-separated preference list tried in order "
        "(e.g. 'http://192.168.1.50:8400,https://box.tailnet.ts.net' — LAN first, "
        "Tailscale as the off-network fallback)"
    ),
)
def setup(token, user, server):
    """First-time setup. Pass --token, --user, --server for non-interactive mode."""
    from .setup_flow import run_setup, run_setup_noninteractive
    if token and user and server:
        run_setup_noninteractive(user=user, token=token, server_url=server)
    else:
        run_setup()


@main.command()
def daemon():
    """Start the daemon (side-car: EventKit + vault watcher + background tasks)."""
    import asyncio
    from .daemon import run_daemon
    asyncio.run(run_daemon())


@main.command()
def status():
    """Show daemon and connection status."""
    from .status import show_status
    show_status()


@main.command()
def install():
    """Install the launchd agent for auto-start."""
    from .launchd import install_agent
    install_agent()


@main.command()
def uninstall():
    """Remove the launchd agent."""
    from .launchd import uninstall_agent
    uninstall_agent()


@main.command("voice-memos")
@click.option("--backfill", is_flag=True, help="Upload existing recordings, not just new ones")
@click.option("--limit", type=int, default=None, help="Stop after this many uploads")
@click.option("--dry-run", is_flag=True, help="List what would be uploaded, upload nothing")
def voice_memos(backfill, limit, dry_run):
    """Inspect or backfill Apple Voice Memos capture.

    With no flags, reports what the watcher can see and how much it has already
    uploaded. `--backfill` uploads recordings already on disk.

    Backfill is a deliberate command rather than something the daemon does at
    startup, because every upload costs a server-side transcription — a fresh
    install auto-uploading years of memos would be an unpleasant surprise.
    Recordings that already carry an on-device Apple transcript cost nothing:
    the server uses that instead of paying (see the transcription integration).

    `--limit` is worth using on a first run to confirm the path end to end
    before committing to the whole backlog.
    """
    from .voice_memos_cli import run_voice_memos
    run_voice_memos(backfill=backfill, limit=limit, dry_run=dry_run)


@main.command("import-health")
@click.argument("file", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Parse and show summary without pushing to server")
def import_health(file, dry_run):
    """Import Apple Health data from a Health Auto Export JSON file."""
    from .health_import import run_import
    run_import(file, dry_run=dry_run)
