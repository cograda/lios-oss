.PHONY: dev build deploy-pull deploy-build deploy-fast deploy-local docker-up docker-down docker-logs init-certs refresh-coglib db-migrate db-upgrade db-downgrade db-history db-current

DEPLOY_HOST = ubuntu    # your server's SSH alias
DEPLOY_DIR = ~/comar-server
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
build:
	cd frontend && npm run build

# ── TLS certificate generation ─────────────────────────────

# Generate self-signed CA + server cert for gRPC and nginx TLS.
# Run once on the server, then distribute ca.crt to clients.
init-certs:
	@mkdir -p certs
	@echo "Generating CA key and cert..."
	openssl genrsa -out certs/ca.key 4096
	openssl req -new -x509 -key certs/ca.key -sha256 \
		-subj "/CN=Comar CA" -days 3650 -out certs/ca.crt
	@echo "Generating server key and CSR..."
	openssl genrsa -out certs/server.key 2048
	openssl req -new -key certs/server.key \
		-subj "/CN=comar-server" -out certs/server.csr
	@echo "Signing server cert with CA..."
	openssl x509 -req -in certs/server.csr \
		-CA certs/ca.crt -CAkey certs/ca.key -CAcreateserial \
		-extfile <(printf "subjectAltName=IP:$(SERVER_IP),DNS:localhost") \
		-days 3650 -sha256 -out certs/server.crt
	@rm -f certs/server.csr certs/ca.srl
	@echo ""
	@echo "Certs generated in certs/:"
	@echo "  ca.crt      — distribute to clients (comar setup)"
	@echo "  server.crt  — server certificate"
	@echo "  server.key  — server private key (keep secret)"

# ── Local Docker (for testing) ─────────────────────────────

docker-up:
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

# Refresh vendored coglib from the upstream monorepo (run manually when coglib changes)
# Also stamps coglib/.vendored-commit with the upstream HEAD sha + ISO date,
# so CI can warn (never fail) if the vendored copy looks stale.
COGLIB_SRC ?= $(HOME)/Desktop/Code/coglib-monorepo
refresh-coglib:
	rsync -avz --delete \
		--exclude='__pycache__' \
		--exclude='.git' \
		--exclude='*.egg-info' \
		--exclude='tests' \
		$(COGLIB_SRC)/coglib/ coglib/
	@{ \
		sha=$$(git -C $(COGLIB_SRC) rev-parse HEAD); \
		date=$$(date -u +%Y-%m-%dT%H:%M:%SZ); \
		printf '%s\n%s\n' "$$sha" "$$date" > coglib/.vendored-commit; \
		echo "Stamped coglib/.vendored-commit: $$sha @ $$date"; \
	}

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
deploy-pull:
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose pull && docker compose up -d"

# Dev rsync deploy: build frontend, sync everything, rebuild + restart on server
deploy-build: build sync
	ssh $(DEPLOY_HOST) "cd $(DEPLOY_DIR) && docker compose up -d --build"

# Dev rsync deploy without rebuilding frontend (backend-only changes)
deploy-fast: sync
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
