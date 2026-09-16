.PHONY: dev build stage-libs sync-contracts deploy-pull verify-deploy deploy-build deploy-fast deploy-local docker-up docker-down docker-logs db-migrate db-upgrade db-downgrade db-history db-current

DEPLOY_HOST = ubuntu
DEPLOY_DIR = ~/lios-core
SERVER_IP ?= 127.0.0.1

# The host directory docker-compose.yml mounts read-only at /contracts (see
# HOME_CONTRACTS_HOST_PATH there) is a plain copy of this repo's contracts/
# directory, not a git checkout — nothing else refreshes it. Measured
# 2026-09-09: it was four days behind main, so a fleet change merged to main
# never reached the device_fleet/host_fleet alerting axes until someone
# rsynced by hand, and the failure is silent (the axis keeps alerting on a
# retired board, or never learns about a new one). Overridable for a
# differently-laid-out host.
CONTRACTS_HOST_PATH ?= /home/alex/lios-contracts

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
# container uses; `until=6h` keeps only the last few hours' previous images.
# It was 24h until 2026-09-05, when six core deploys in one night took the
# host from 65 % to 83 % (the alert fires at 85 %) with the 24h window doing
# nothing, because every image was younger than a day. Rollback was never the
# reason to keep them: anything is re-pullable from GHCR by its :<sha> tag —
# the rollback path is the registry, not the disk.
#
# lios#219 — a deploy interrupted mid-recreate (the orchestrating tool call
# died, 2026-09-11 ~21:56-22:07 UTC) left lios-core in state `Created`, never
# started, for ~10 minutes with nothing noticing — Prometheus scraped the
# monitoring stack, never the app itself (see prometheus.yml's blackbox job,
# added the same issue, for the standing fix). This is the deploy-time half:
# after `docker compose up -d`, poll (bounded, ~90s) for the container to
# reach `Up` AND for /api/health to answer 200, and exit non-zero loudly —
# with the container's own recent logs printed — if it never does, rather
# than reporting success because the `up -d` command itself returned 0 (that
# command only starts a recreate; it does not wait for the result).
# The compose file is deploy config, not image content — a change to it on
# `main` (a new env mapping, a new volume) is invisible to the box until
# something copies it there. Until 2026-09-15 only the dev-path `sync` did,
# so #240's HOME_ALERTS_INLET_KEY mapping shipped in the image build and
# never reached the host: the container came up with no key and the inlet
# answered 503. Ship the one file every pull deploy.
sync-compose:
	rsync -az docker-compose.yml $(DEPLOY_HOST):$(DEPLOY_DIR)/docker-compose.yml

deploy-pull: sync-contracts sync-compose
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose pull && docker compose up -d && docker image prune -af --filter until=6h | tail -1 && df -h / | tail -1"
	$(MAKE) verify-deploy

# Poll for lios-core actually being up and healthy after a deploy. Bounded
# at ~90s (30 tries, 3s apart) so a genuinely stuck deploy fails the CI/
# operator step loudly instead of hanging or (worse) silently succeeding.
# Runs over ssh against the deploy host, not this machine, since neither
# `docker compose ps` nor the app's health endpoint is reachable from
# outside the LAN/tailnet by design (see core/CLAUDE.md's Transport stance).
verify-deploy:
	@ssh $(DEPLOY_HOST) ' \
		set -eu; \
		cd $(DEPLOY_DIR); \
		ok=0; \
		for i in $$(seq 1 30); do \
			state=$$(docker compose ps --format "{{.State}}" app 2>/dev/null || echo "?"); \
			if [ "$$state" = "running" ]; then \
				code=$$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://localhost:8400/api/health || echo "000"); \
				if [ "$$code" = "200" ]; then ok=1; break; fi; \
			fi; \
			sleep 3; \
		done; \
		if [ "$$ok" != "1" ]; then \
			echo "verify-deploy: FAILED — lios-core did not reach Up + /api/health 200 within ~90s." >&2; \
			echo "  Last observed compose state: $$state" >&2; \
			echo "  --- docker compose logs --tail=50 app ---" >&2; \
			docker compose logs --tail=50 app >&2 || true; \
			exit 1; \
		fi; \
		echo "verify-deploy: OK — lios-core Up, /api/health 200." \
	'

# Dev rsync deploy: build frontend, sync everything, rebuild + restart on server
deploy-build: build sync
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose up -d --build"

# Dev rsync deploy without rebuilding frontend (backend-only changes)
deploy-fast: stage-libs sync sync-contracts
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose up -d --build app"

# ── contracts/: refresh the host copy the compose mount reads ─────
#
# docker-compose.yml mounts $(CONTRACTS_HOST_PATH) read-only at /contracts so
# device_fleet/host_fleet can read contracts/fleet.md + contracts/hosts.md.
# That host path is a plain rsync copy, not a git checkout, so nothing pulls
# it forward on its own — see the comment above CONTRACTS_HOST_PATH. Run this
# on every deploy so a merged fleet change always reaches the alerting axis
# rather than silently staying stale.
sync-contracts:
	rsync -az --delete $(abspath ../../contracts)/ $(DEPLOY_HOST):$(CONTRACTS_HOST_PATH)/
	@echo "sync-contracts: synced $(abspath ../../contracts)/ to $(DEPLOY_HOST):$(CONTRACTS_HOST_PATH)/"

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
