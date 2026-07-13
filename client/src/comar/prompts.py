"""Prompt store — loads MCP prompt definitions from YAML files.

Prompts expose orchestration flows (daily note, meeting processing, etc.)
to Claude Desktop via the MCP prompts capability. Each prompt is a YAML
file with name, description, arguments, and message templates.

The store is mutable: new prompts can be synced from the server at
runtime without restarting the daemon.
"""

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from importlib import resources as pkg_resources
from pathlib import Path

import yaml
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
)

logger = logging.getLogger(__name__)


@dataclass
class PromptDefinition:
    """A single prompt parsed from a YAML file."""

    name: str
    title: str = ""
    description: str = ""
    arguments: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    checksum: str = ""

    def to_mcp_prompt(self) -> Prompt:
        """Convert to an MCP Prompt object for list_prompts."""
        args = [
            PromptArgument(
                name=a["name"],
                description=a.get("description"),
                required=a.get("required", False),
            )
            for a in self.arguments
        ] or None

        return Prompt(
            name=self.name,
            title=self.title or None,
            description=self.description or None,
            arguments=args,
        )

    def render(self, arguments: dict[str, str]) -> GetPromptResult:
        """Render the prompt with given arguments into a GetPromptResult."""
        rendered_messages = []
        for msg in self.messages:
            content = msg.get("content", "")
            # Simple template substitution: {{ arg_name }}
            for arg_def in self.arguments:
                arg_name = arg_def["name"]
                placeholder = "{{ " + arg_name + " }}"
                value = arguments.get(arg_name, arg_def.get("default", ""))
                content = content.replace(placeholder, str(value))
            rendered_messages.append(
                PromptMessage(
                    role=msg.get("role", "user"),
                    content=TextContent(type="text", text=content),
                )
            )

        return GetPromptResult(
            description=self.description or None,
            messages=rendered_messages,
        )


class PromptStore:
    """Loads, caches, and serves MCP prompt definitions from a directory.

    On first load, if the target directory is empty, copies bundled
    default prompts from the package. Supports runtime reload for
    two-tier update sync from the server.
    """

    def __init__(self):
        self._prompts: dict[str, PromptDefinition] = {}
        self._dir: Path | None = None

    def load_from_dir(self, path: Path) -> None:
        """Load all prompt YAML files from a directory.

        If the directory is empty or doesn't exist, copies bundled
        defaults first.
        """
        self._dir = path
        path.mkdir(parents=True, exist_ok=True)

        # Seed defaults if empty
        yaml_files = list(path.glob("*.yaml")) + list(path.glob("*.yml"))
        if not yaml_files:
            self._seed_defaults(path)
            yaml_files = list(path.glob("*.yaml")) + list(path.glob("*.yml"))

        self._prompts.clear()
        for f in sorted(yaml_files):
            try:
                defn = self._parse_file(f)
                self._prompts[defn.name] = defn
            except Exception:
                logger.exception(f"Failed to parse prompt file: {f.name}")

        logger.info(f"Loaded {len(self._prompts)} prompts from {path}")

    def list_prompts(self) -> list[Prompt]:
        """Return MCP Prompt objects for all loaded prompts."""
        return [defn.to_mcp_prompt() for defn in self._prompts.values()]

    def get_prompt(self, name: str, arguments: dict[str, str]) -> GetPromptResult:
        """Render and return a prompt by name."""
        defn = self._prompts.get(name)
        if defn is None:
            raise ValueError(f"Unknown prompt: {name}")
        return defn.render(arguments)

    def prompt_set_hash(self) -> str:
        """SHA256 hash of all prompt checksums — for change detection."""
        checksums = sorted(d.checksum for d in self._prompts.values())
        combined = ":".join(checksums)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    @property
    def count(self) -> int:
        return len(self._prompts)

    def sync_from_server(self, prompt_defs: list[dict]) -> int:
        """Write new/changed prompts from server, remove deleted ones, reload.

        Each item in prompt_defs should have: name, yaml_content, checksum.
        Returns the number of prompts changed.
        """
        if self._dir is None:
            return 0

        server_names = set()
        changed = 0

        for pdef in prompt_defs:
            name = pdef["name"]
            if not name or "/" in name or "\\" in name or ".." in name:
                logger.warning(f"Skipping prompt with unsafe name from server: {name!r}")
                continue
            server_names.add(name)
            target = self._dir / f"{name}.yaml"

            # Check if local file exists and matches
            if target.is_file():
                local_checksum = self._file_checksum(target)
                if local_checksum == pdef["checksum"]:
                    continue

            # Write new/changed file
            target.write_text(pdef["yaml_content"], encoding="utf-8")
            changed += 1
            logger.info(f"Prompt synced from server: {name}")

        # Remove prompts deleted from server
        for f in self._dir.glob("*.yaml"):
            stem = f.stem
            if stem not in server_names:
                f.unlink()
                changed += 1
                logger.info(f"Prompt removed (deleted on server): {stem}")

        if changed:
            self.load_from_dir(self._dir)

        return changed

    # -- Internal helpers --

    @staticmethod
    def _parse_file(path: Path) -> PromptDefinition:
        """Parse a single prompt YAML file."""
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid prompt file (not a dict): {path.name}")

        checksum = hashlib.sha256(raw.encode()).hexdigest()[:16]

        return PromptDefinition(
            name=data["name"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            arguments=data.get("arguments", []),
            messages=data.get("messages", []),
            checksum=checksum,
        )

    @staticmethod
    def _file_checksum(path: Path) -> str:
        """Compute SHA256 checksum of a file (truncated to 16 chars)."""
        raw = path.read_bytes()
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _seed_defaults(target_dir: Path) -> None:
        """Copy bundled default prompts into the target directory."""
        # default_prompts/ lives alongside this module in the package
        defaults_dir = Path(__file__).parent / "default_prompts"
        if not defaults_dir.is_dir():
            logger.warning(f"No bundled default prompts found at {defaults_dir}")
            return

        count = 0
        for f in defaults_dir.glob("*.yaml"):
            dest = target_dir / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                count += 1

        if count:
            logger.info(f"Seeded {count} default prompts into {target_dir}")
