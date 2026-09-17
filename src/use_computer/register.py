"""Register the MCP server with Claude Code by editing ~/.claude.json.

`claude mcp add --scope user` writes a top-level `mcpServers` entry in that file,
and the desktop app's Code tab reads it too, so the CLI isn't needed. Claude Code
rewrites the file while running and there is no lock, so: back it up, re-read
immediately before writing, write to a temp file and rename, and retry if the
file changed underneath us.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from . import UseComputerError

NAME = "use-computer"


def config_path() -> Path:
    return Path(os.environ.get("USE_COMPUTER_CLAUDE_JSON", Path.home() / ".claude.json"))


def _edit(mutate: Any) -> bool:
    path = config_path()
    for _ in range(5):
        if path.exists():
            before = path.stat().st_mtime_ns
            raw = path.read_text(encoding="utf-8")
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                raise UseComputerError(f"{path} is not valid JSON; not touching it ({exc})") from exc
        else:
            before, raw, data = None, "", {}
        if not mutate(data):
            return False
        if raw:
            backups = path.parent / ".claude" / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backups / f".claude.json.use-computer.{int(time.time())}")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".claude.json.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        now = path.stat().st_mtime_ns if path.exists() else None
        if now != before:
            os.unlink(tmp)
            time.sleep(0.2)
            continue
        os.replace(tmp, path)
        return True
    raise UseComputerError(f"{path} kept changing while editing; try again")


def register(command: str) -> str:
    entry = {"type": "stdio", "command": command, "args": [], "env": {}}

    def mutate(data: dict[str, Any]) -> bool:
        servers = data.setdefault("mcpServers", {})
        if servers.get(NAME) == entry:
            return False
        servers[NAME] = entry
        return True

    changed = _edit(mutate)
    state = "registered" if changed else "already registered"
    return f"{NAME} MCP server {state} in {config_path()} -> {command} (start a new Claude session)"


def unregister() -> str:
    def mutate(data: dict[str, Any]) -> bool:
        return data.get("mcpServers", {}).pop(NAME, None) is not None

    changed = _edit(mutate)
    return f"{NAME} MCP server {'removed from' if changed else 'was not in'} {config_path()}"
