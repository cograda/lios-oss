"""F-security: uvicorn must trust X-Forwarded-For only from the Caddy
sidecar's own Docker network — otherwise every rate-limit/audit `source_ip`
(app/routes/auth.py, app/auth/rate_limit.py) reads Caddy's container IP for
every browser caller, so a few failed logins through Caddy lock out every
:443 caller at once (see the Dockerfile CMD comment for the full story).

This is a static check on the committed Dockerfile rather than a live
uvicorn boot — the property we care about is "the flag is actually in the
image's entrypoint", which a running-process test can't see any more
directly than reading the file that builds it.
"""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def _cmd_line() -> str:
    text = DOCKERFILE.read_text()
    assert "CMD [" in text, "Dockerfile has no CMD — did it move?"
    # The CMD may span multiple lines (line continuation); grab from "CMD ["
    # to the closing "]" of that instruction, whichever line it ends on.
    start = text.index("CMD [")
    end = text.index("]", start)
    return text[start:end + 1]


class TestUvicornProxyHeaders:
    def test_dockerfile_exists(self):
        assert DOCKERFILE.is_file()

    def test_cmd_enables_proxy_headers(self):
        cmd = _cmd_line()
        assert "--proxy-headers" in cmd

    def test_cmd_scopes_forwarded_allow_ips_not_wildcard(self):
        """Must be present, and must NOT be '*' — trusting every caller's
        X-Forwarded-For would let a direct :8400 caller spoof any IP and
        evade (or weaponise) the login rate limiter."""
        cmd = _cmd_line()
        assert "--forwarded-allow-ips=" in cmd
        assert "--forwarded-allow-ips=*" not in cmd

    def test_forwarded_allow_ips_matches_documented_bridge_subnet(self):
        """The allow-list is the `homelab_default` bridge subnet, evidenced
        by app/config.py's `syncthing_url` comment recording the bridge
        gateway address this same container already reaches on that
        network (172.21.0.1) — see the Dockerfile comment for the citation."""
        cmd = _cmd_line()
        assert "--forwarded-allow-ips=172.21.0.0/16" in cmd

    def test_still_binds_all_interfaces_on_container_port(self):
        cmd = _cmd_line()
        assert '"--host", "0.0.0.0"' in cmd
        assert '"--port", "8000"' in cmd


def test_dockerfile_pins_postgresql_client_to_the_server_major():
    """Debian 13's own postgresql-client is 17; the server is 16. A mismatched
    pg_restore broke the restore drill's first live run (2026-09-04)."""
    text = DOCKERFILE.read_text()
    assert "postgresql-client-16" in text
    assert "apt.postgresql.org" in text
    # the unversioned package would silently float to whatever Debian ships
    install_lines = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"postgresql-client(\s|$)", install_lines.replace("postgresql-client-16", "")), \
        "unversioned postgresql-client would float with the base image"
