"""Loads the MCP server list from a Claude-Desktop-style `mcpServers` JSON file, or from environment
variables in the shape Agent Manager provides for MCP servers attached to an agent."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]*$")

# The servers the concierge expects, as Agent Manager names their variables: <PREFIX>_URL holds one or
# more comma-separated MCP endpoint URLs and <PREFIX>_API_KEY an optional gateway API key for them.
DEFAULT_ENV_PREFIXES = ("HOTEL_INFO", "RESERVATIONS", "UNITY_LOYALTY")
API_KEY_HEADER = "API-Key"


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


def load_env_servers(prefixes: Sequence[str], environ: Mapping[str, str] | None = None) -> list[ServerConfig]:
    """Build server configs from `<PREFIX>_URL` and an optional `<PREFIX>_API_KEY`.

    Each URL becomes one Streamable HTTP server named after the prefix (`UNITY_LOYALTY` ->
    `unity-loyalty`, with `-1`, `-2`... when a variable lists several URLs). A key, when set, is sent
    in the `API-Key` header; proxies that authenticate with JWTs need none, since every request
    carries `Authorization: Bearer <token>` (see `hub.BearerAuth`). A prefix without a URL is
    skipped, so an agent can attach a subset.

    Raises:
        ConfigError: if a prefix is malformed, a key has no URL, or no server is configured.
    """
    env = os.environ if environ is None else environ
    configs: list[ServerConfig] = []
    for prefix in prefixes:
        if not _PREFIX.match(prefix):
            raise ConfigError(f"MCP env prefix {prefix!r} must be upper-case letters, digits and underscores")
        urls = [url.strip() for url in env.get(f"{prefix}_URL", "").split(",") if url.strip()]
        api_key = env.get(f"{prefix}_API_KEY", "").strip()
        if not urls:
            if api_key:
                raise ConfigError(f"{prefix}_URL is not set, but {prefix}_API_KEY is")
            continue
        name = prefix.lower().replace("_", "-")
        headers = {API_KEY_HEADER: api_key} if api_key else {}
        for index, url in enumerate(urls, start=1):
            server_name = name if len(urls) == 1 else f"{name}-{index}"
            configs.append(ServerConfig(name=server_name, url=url, headers=headers))
    if not configs:
        expected = ", ".join(f"{prefix}_URL" for prefix in prefixes)
        raise ConfigError(f"no MCP servers configured in the environment; expected some of: {expected}")
    return configs
