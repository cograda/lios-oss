"""Structured logging setup from LoggingConfig."""

from __future__ import annotations

import logging
import sys

from coglib.config import LoggingConfig


def setup_logging(config: LoggingConfig | None = None) -> None:
    """Configure root logger from a LoggingConfig instance."""
    if config is None:
        config = LoggingConfig()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    if config.file is not None:
        config.file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config.file))

    logging.basicConfig(
        level=config.level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
