#!/bin/bash
# Wrapper for launchd — uses anaconda python which has requests
export PATH="/opt/anaconda3/bin:$PATH"
cd "$(dirname "$0")"
exec python3 sync.py
