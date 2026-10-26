"""Loads the MCP server list from a Claude-Desktop-style `mcpServers` JSON file."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """The server config file is missing, malformed, or references an unset variable."""


@dataclass(frozen=True)
class ServerConfig:
    """One MCP server: either a Streamable HTTP `url` or a stdio `command`."""

    name: str
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: Path | None = None


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> list[ServerConfig]:
    """Parse `path` into server configs, expanding `${VAR}` references in urls, headers and args.

    Raises:
        ConfigError: if the file cannot be read or an entry is invalid.
    """
    env = os.environ if environ is None else environ
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read MCP config {path}: {exc}") from exc

    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ConfigError(f"{path} must contain a non-empty 'mcpServers' object")
    return [_parse_entry(name, entry, path.parent, env) for name, entry in servers.items()]


def _parse_entry(name: str, entry: object, base_dir: Path, env: Mapping[str, str]) -> ServerConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"server {name!r} must be an object")
    url, command = entry.get("url"), entry.get("command")
    if bool(url) == bool(command):
        raise ConfigError(f"server {name!r} needs exactly one of 'url' or 'command'")
    if url:
        headers = {key: _expand(value, env, name) for key, value in entry.get("headers", {}).items()}
        return ServerConfig(name=name, url=_expand(url, env, name), headers=headers)
    args = tuple(_expand(arg, env, name) for arg in entry.get("args", []))
    return ServerConfig(name=name, command=command, args=args, cwd=base_dir)


def _expand(value: str, env: Mapping[str, str], server: str) -> str:
    def lookup(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in env:
            raise ConfigError(f"server {server!r} references ${{{var}}}, which is not set")
        return env[var]

    return _ENV_REF.sub(lookup, value)
