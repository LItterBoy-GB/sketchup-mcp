"""Read the local SketchUp MCP listener registry without probing arbitrary ports."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List


REGISTRY_SCHEMA_VERSION = 1


def registry_dir() -> Path:
    """Return the directory written by the Ruby SketchUp extension."""
    return Path(tempfile.gettempdir()) / "sketchup-mcp" / "instances"


def load_registered_instances() -> List[Dict[str, Any]]:
    """Return syntactically valid registry entries, without modifying them."""
    directory = registry_dir()
    if not directory.is_dir():
        return []

    entries: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if not isinstance(payload, dict):
            continue

        try:
            schema_version = int(payload["schema_version"])
            port = int(payload["port"])
            pid = int(payload["pid"])
        except (KeyError, TypeError, ValueError):
            continue

        if schema_version != REGISTRY_SCHEMA_VERSION or not 1 <= port <= 65_535 or pid <= 0:
            continue

        entries.append({**payload, "port": port, "pid": pid, "registry_path": str(path)})

    return entries
