"""Minimal `.env` loader: `KEY=value` lines, `#` comments, optional surrounding quotes."""

from __future__ import annotations

import os
from collections.abc import Iterable, MutableMapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILES = (PROJECT_ROOT / ".env.thunder", PROJECT_ROOT / ".env")


def parse_env_file(text: str) -> dict[str, str]:
    """Parse dotenv text. Lines without `=` are ignored."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_env_files(paths: Iterable[Path] = DEFAULT_ENV_FILES, environ: MutableMapping[str, str] | None = None) -> None:
    """Copy values from the files into `environ` without overriding variables already set."""
    target = os.environ if environ is None else environ
    for path in paths:
        if not path.is_file():
            continue
        for key, value in parse_env_file(path.read_text(encoding="utf-8")).items():
            target.setdefault(key, value)
