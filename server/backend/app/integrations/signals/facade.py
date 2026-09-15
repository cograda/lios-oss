"""signals' facade — the only surface kernel code may import from directly
(`tests/test_kernel_import_guard.py`). No declared `provides` capability
today (see `manifest.py`'s note on that); this exists purely so
`app/main.py`'s startup doesn't reach into `routes.py`'s internals to install
the access-log redaction filter.
"""

from __future__ import annotations

from app.integrations.signals.routes import install_log_filters

__all__ = ["install_log_filters"]
