.PHONY: dev build stage-libs deploy-pull deploy-build deploy-fast deploy-local docker-up docker-down docker-logs db-migrate db-upgrade db-downgrade db-history db-current

DEPLOY_HOST = ubuntu
DEPLOY_DIR = ~/lios-core
SERVER_IP ?= 127.0.0.1

# ── Database migrations (Alembic) ─────────────────────────

# Generate a new migration from model changes (run locally)
# Usage: make db-migrate msg="add source_id columns"
db-migrate:
	cd backend && HOME_DATABASE__URL=$${HOME_DATABASE__URL:-postgresql://homeservices:homeservices@localhost:5432/home_services} \
		.venv/bin/alembic revision --autogenerate -m "$(msg)"

# Run pending migrations (on server via Docker)
db-upgrade:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose exec app alembic -c /app/backend/alembic.ini upgrade head"

# Roll back one migration (on server via Docker)
db-downgrade:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose exec app alembic -c /app/backend/alembic.ini downgrade -1"

# Show migration history
db-history:
	cd backend && .venv/bin/alembic history --verbose

# Show current revision (on server via Docker)
db-current:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose exec app alembic -c /app/backend/alembic.ini current"

# ── Local development ──────────────────────────────────────

# Start both frontend and backend dev servers
dev:
	@echo "Starting backend..."
	cd backend && source .venv/bin/activate && uvicorn app.main:app --reload --port 8000 &
	@echo "Starting frontend..."
	cd frontend && npm run dev

# Build frontend for production
build: stage-libs
	cd frontend && npm run build

# ── Local Docker (for testing) ─────────────────────────────

docker-up: stage-libs
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app

# ── Deploy to home server ──────────────────────────────────

# Sync project files to home server
sync:
	rsync -avz --delete \
		--exclude='node_modules' \
		--exclude='.git' \
		--exclude='__pycache__' \
		--exclude='.venv' \
		--exclude='.env' \
		--exclude='certs' \
		. $(DEPLOY_HOST):$(DEPLOY_DIR)/

# ── coglib: staged at build time, never copied into git ─────────────
#
# Until 2026-09-02 coglib was VENDORED here: `make refresh-coglib` rsynced
# lios/libs/coglib into server/coglib/, stamped it, and a test + a CI step
# checked the copy against its stamp. That loop existed because coglib lived in
# a different repo (alexunism) and could drift between commits. It cannot any
# more: coglib and its consumer are in the same repo and the same commit, so
# the vendored tree was a copy of itself — the exact second editable copy the
# monorepo exists to eliminate.
#
# The Docker build context is `core/server` (widening it to the repo root
# would put the gitignored vault inside every build context — see
# .github/workflows/core-deploy.yml), so the Dockerfile cannot reach
# ../../libs. `stage-libs` copies coglib into the gitignored .libs/ directory
# immediately before a build, the same pattern apps/dashboard uses for
# libs/ha. Nothing under .libs/ is ever committed.
#
# COGLIB_SRC resolves to lios/libs/coglib. The fallback to ./coglib exists for
# ONE consumer: the public comar-oss release, where prepare-release.sh lays a
# copy of libs/coglib at server/coglib/ because that repo has no libs/ above
# it. Inside lios neither path being present is a hard error, not a skip — an
# absent sibling directory means the path is wrong, and skipping would report
# success for a build that has no coglib in it.
LIOS_ROOT := $(shell git rev-parse --show-toplevel)
COGLIB_SRC ?= $(if $(wildcard $(LIOS_ROOT)/libs/coglib/src/coglib),$(LIOS_ROOT)/libs/coglib,./coglib)
stage-libs:
	@if [ ! -d "$(COGLIB_SRC)/src/coglib" ]; then \
		echo "stage-libs: FAILED — no coglib source at $(COGLIB_SRC)." >&2; \
		echo "  Expected lios/libs/coglib (or ./coglib in a comar-oss checkout)." >&2; \
		echo "  This is a hard error: an image built without coglib would not start." >&2; \
		exit 1; \
	fi
	rm -rf .libs
	mkdir -p .libs/coglib
	rsync -a --exclude tests --exclude '__pycache__' --exclude '*.egg-info' \
		--exclude '.pytest_cache' --exclude '.mypy_cache' --exclude '.ruff_cache' \
		"$(COGLIB_SRC)/" .libs/coglib/
	@echo "stage-libs: coglib staged from $(COGLIB_SRC) into .libs/coglib"

# ── Which deploy target to use ─────────────────────────────
# deploy-pull   DEFAULT (root `make deploy` points here). Pulls the pre-built
#               GHCR images that CI pushed on merge to main, then restarts.
#               Use this for anything that's been committed and pushed.
# deploy-build  Dev-only rsync path: builds frontend locally, rsyncs the whole
#               working tree to the server and rebuilds images there. Use only
#               to test uncommitted changes on the box without going through
#               CI. (Formerly `deploy`.)
# deploy-fast   Same rsync path but skips the frontend build — backend-only
#               uncommitted changes.

# Deploy using pre-built GHCR images (after CI pushes them) — preferred path
# ⚠️ The prune is not housekeeping. Every deploy pulls two images tagged
# :latest AND :<sha>, so the previous ones stay tagged and are never dangling;
# seven deploys on 2026-09-02 took the 61 GB host to 100 % and Postgres
# PANICked mid-checkpoint ("No space left on device") and crash-looped in
# recovery until images were pruned. `-a` removes every image no running
# container uses; `until=24h` keeps the most recent previous images, so the
# usual same-day rollback is still local. Anything older is re-pullable from
# GHCR by its :<sha> tag — the rollback path is the registry, not the disk.
deploy-pull:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose pull && docker compose up -d && docker image prune -af --filter until=24h | tail -1 && df -h / | tail -1"

# Dev rsync deploy: build frontend, sync everything, rebuild + restart on server
deploy-build: build sync
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose up -d --build"

# Dev rsync deploy without rebuilding frontend (backend-only changes)
deploy-fast: stage-libs sync
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose up -d --build app"

# View logs on home server
server-logs:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose logs -f app"

# Check status on home server
server-status:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose ps"

# Restart on home server
server-restart:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose restart app"

# Stop everything on home server
server-down:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose down"

# First-time setup on home server: create dir, copy .env
server-init:
	ssh $(DEPLOY_HOST) "mkdir -p $(DEPLOY_DIR)"
	scp .env.example $(DEPLOY_HOST):$(DEPLOY_DIR)/.env
	@echo "Edit .env on the server: ssh $(DEPLOY_HOST) 'nano $(DEPLOY_DIR)/.env'"
