FROM python:3.12-slim

WORKDIR /app

# Install system deps for psycopg2
# `wamerican` provides /usr/share/dict/words. The transcription dictionary
# uses it to tell a name worth biasing toward (Malahide, Crannarc) from an
# ordinary English word (Focus, Backlog) — see
# app/integrations/transcription/dictionary.py. python:3.12-slim ships no
# wordlist, and without one that whole feature degrades to a silent no-op:
# nothing errors, the prompt just quietly loses ~150 terms. ~1 MB.
# postgresql-client (pg_restore/createdb/dropdb/psql) is for the weekly
# restore drill (R2, 2026-09-05 — app/integrations/system/restore_drill.py),
# which runs `docker compose exec app python -m
# app.integrations.system.restore_drill` from a host cron. libpq-dev alone
# (needed to build psycopg2) does not ship those client binaries.
# ⚠️ The client MAJOR must match the server's (PostgreSQL 16, `home-services-db`
# in deploy/docker-compose.yml). python:3.12-slim is Debian 13, whose own
# `postgresql-client` is 17, and a 17 pg_restore emits `SET transaction_timeout
# = 0`, which a 16 server rejects — the drill's first live run (2026-09-04)
# failed exactly there. So the client comes from the PostgreSQL apt repo,
# pinned to 16; restore_drill.py also pre-flights the majors and names a
# mismatch. Bump both together when the server moves to 17.
# ffmpeg: the `signals` integration's frame grabs (RTSP -> JPEG, the same
# `-rtsp_transport tcp` incantation house/patio-timelapse/grab_frame.py
# proved) and its ROI crop/downscale/brightness helpers all shell out to it.
# No Python binding — same "one binary, no library" shape as the timelapse
# script this borrows from.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev wamerican ca-certificates curl ffmpeg && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc && \
    . /etc/os-release && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 && \
    apt-get purge -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# F9 (hardening-2026-08): don't run the app as root. Fixed uid/gid 1000,
# chosen to match the host `alex` account (uid 1000) that owns every
# bind-mounted volume (vaults, doc-corpus, media) — see server/CLAUDE.md
# "Deployment" and the F9 deploy notes for the one-time chown needed on the
# two NAMED (non-bind) volumes, inbox_data and fastembed_cache, which predate
# this change and were created world-writable under the old root process.
RUN groupadd -g 1000 comar && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin comar

# coglib lives at lios/libs/coglib, outside this build context (`core/server`
# — widening it would put the gitignored vault inside every build). `make
# stage-libs` copies it into the gitignored .libs/ directory immediately before
# any build; CI runs the same target. A build without that step fails HERE,
# loudly, rather than producing an image that starts and cannot import coglib.
COPY .libs/coglib/ /tmp/coglib/
RUN pip install --no-cache-dir /tmp/coglib/ && rm -rf /tmp/coglib/

# Install Python deps (coglib already installed above)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fastembed model cache directory — create + chown now so a brand-new named
# volume mounted here inherits comar:comar ownership on first run (Docker
# seeds a new named volume from the image directory it overlays).
ENV FASTEMBED_CACHE_PATH=/app/.cache/fastembed
RUN mkdir -p /app/.cache/fastembed && chown -R comar:comar /app/.cache

# Copy backend
COPY --chown=comar:comar backend/ backend/

# Copy frontend build (built on host)
COPY --chown=comar:comar frontend/dist/ frontend/dist/

# Copy built client wheels so /api/client/download/latest can serve them to daemons.
# Without this the auto-updater finds no wheel and silently never advances. See ISS-015.
COPY --chown=comar:comar client-dist/ client-dist/

EXPOSE 8000

USER comar
WORKDIR /app/backend
# F-security: this container sits behind Caddy (deploy/docker-compose.yml,
# `reverse_proxy lios-core:8000`) on the shared `homelab_default` bridge
# network. Without --proxy-headers, uvicorn treats every request's peer as
# Caddy's own container IP — `request.client.host` in app/routes/auth.py's
# login rate limiter (and the `source_ip=` on auth_events rows) then keys
# on Caddy's IP for every browser, so a handful of failed logins through
# Caddy locks out every :443 caller at once while a direct :8400 caller is
# unaffected.
#
# --forwarded-allow-ips is deliberately NOT "*": trusting every caller's
# X-Forwarded-For would let a direct :8400 caller (LAN/tailnet) spoof any
# IP and evade — or weaponise — the same rate limiter. It's scoped to
# 172.21.0.0/16, the subnet Docker actually assigned `homelab_default` —
# see app/config.py's `syncthing_url` comment, which records the bridge
# gateway address (172.21.0.1) this container already reaches Syncthing
# through on that same network. Only a peer inside that range (i.e. Caddy)
# gets its X-Forwarded-For trusted; anything else is treated as the direct
# caller it is.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=172.21.0.0/16"]
