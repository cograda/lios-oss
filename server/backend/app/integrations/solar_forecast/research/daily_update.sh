#!/usr/bin/env bash
# Nightly job: pull yesterday's completed data, re-fit, log the eval.
# Installed via ~/Library/LaunchAgents/ie.comar.solar-daily-research.plist —
# see README.md. Uses core/server/backend/.venv (has scikit-learn + numpy),
# not house/homeassistant/.venv (has websockets + pyyaml, no sklearn).
set -euo pipefail
cd "$(dirname "$0")"

set -a && . ~/.airq/ha.env && set +a

PY="../../../../.venv/bin/python"
[ -x "$PY" ] || PY=python3

"$PY" build_dataset.py
"$PY" fit_model.py
