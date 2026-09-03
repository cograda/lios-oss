FROM python:3.12-slim

WORKDIR /app

# Install system deps for psycopg2
# `wamerican` provides /usr/share/dict/words. The transcription dictionary
# uses it to tell a name worth biasing toward (Malahide, Crannarc) from an
# ordinary English word (Focus, Backlog) — see
# app/integrations/transcription/dictionary.py. python:3.12-slim ships no
# wordlist, and without one that whole feature degrades to a silent no-op:
# nothing errors, the prompt just quietly loses ~150 terms. ~1 MB.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev wamerican && \
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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
