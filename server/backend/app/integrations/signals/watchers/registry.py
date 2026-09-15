"""Every registered watcher. Adding a second watcher is a class + an entry
here — see `milk.py` for the shape, and the design note's "what a second
watcher would take" section.
"""

from __future__ import annotations

from app.integrations.signals.watchers.milk import MILK_WATCHER

WATCHERS = [MILK_WATCHER]
