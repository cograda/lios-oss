# Comar — top-level Makefile
#
# Usage:
#   make deploy        Deploy server via pre-built GHCR images (CI-built; preferred)
#   make deploy-build  Dev rsync deploy: build locally, rsync + build on server
#   make deploy-fast   Dev rsync deploy (backend only, skip frontend build)

.PHONY: build-client test test-client test-server deploy deploy-build deploy-fast deploy-pull

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test: test-client test-server

test-client:
	# No client/.venv on this machine yet; falls back to PATH python.
	cd client && python -m pytest tests/ -v

test-server:
	cd server/backend && .venv/bin/python -m pytest tests/ -v

# ---------------------------------------------------------------------------
# Client wheel build
# ---------------------------------------------------------------------------

CLIENT_DIST = server/client-dist

build-client:
	@mkdir -p $(CLIENT_DIST)
	cd client && python -m build --wheel --outdir ../$(CLIENT_DIST)/
	@echo "Client wheel built in $(CLIENT_DIST)/"

# ---------------------------------------------------------------------------
# Server deployment (delegates to server/Makefile)
# ---------------------------------------------------------------------------

# Default deploy: pull the GHCR images CI built (no local build, no rsync).
deploy:
	$(MAKE) -C server deploy-pull

deploy-pull: deploy

# Dev rsync path — only for testing uncommitted changes on the box.
deploy-build: build-client
	$(MAKE) -C server deploy-build

deploy-fast:
	$(MAKE) -C server deploy-fast
