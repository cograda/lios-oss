"""Comar CLI — entry point for all client operations.

Usage:
    comar setup           Interactive first-time setup
    comar daemon          Start the daemon (MCP server + HTTP server client)
    comar status          Show daemon and connection status
    comar install         Install/update the launchd agent
    comar uninstall       Remove the launchd agent
    comar import-health   Import Apple Health data from a JSON file
"""

import click


@click.group()
@click.version_option()
def main():
    """Comar — Co-Managed Archive. Local client for the family knowledge system."""
    pass


@main.command()
@click.option("--token", help="Server auth token (skips interactive mode)")
@click.option("--user", help="Username (alex or sam)")
@click.option("--server", help="Server base URL (e.g. http://192.168.1.50:8400 or https://comar.lab)")
def setup(token, user, server):
    """First-time setup. Pass --token, --user, --server for non-interactive mode."""
    from .setup_flow import run_setup, run_setup_noninteractive
    if token and user and server:
        run_setup_noninteractive(user=user, token=token, server_url=server)
    else:
        run_setup()


@main.command()
def daemon():
    """Start the daemon (MCP server + HTTP server client + background tasks)."""
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


@main.command("import-health")
@click.argument("file", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Parse and show summary without pushing to server")
def import_health(file, dry_run):
    """Import Apple Health data from a Health Auto Export JSON file."""
    from .health_import import run_import
    run_import(file, dry_run=dry_run)
