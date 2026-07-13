"""Server-side prompt registry — source of truth for MCP prompt definitions.

Reads YAML prompt files from the prompts/ directory (alongside this module)
and serves them to clients via the ListPrompts gRPC RPC. The server is the
authoritative source; clients sync from here on each heartbeat.
"""

import hashlib
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent


def get_all_prompts() -> list[dict]:
    """Read all prompt YAML files and return their metadata.

    Returns list of dicts with keys: name, yaml_content, checksum.
    """
    prompts = []
    for f in sorted(_PROMPTS_DIR.glob("*.yaml")):
        try:
            raw = f.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
            if not isinstance(data, dict) or "name" not in data:
                logger.warning(f"Skipping invalid prompt file: {f.name}")
                continue
            checksum = hashlib.sha256(raw.encode()).hexdigest()[:16]
            prompts.append({
                "name": data["name"],
                "yaml_content": raw,
                "checksum": checksum,
            })
        except Exception:
            logger.exception(f"Failed to read prompt file: {f.name}")

    return prompts


def get_prompt_set_hash() -> str:
    """Combined hash of all prompt checksums — for change detection."""
    prompts = get_all_prompts()
    checksums = sorted(p["checksum"] for p in prompts)
    combined = ":".join(checksums)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
