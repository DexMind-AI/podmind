"""Secret storage.

Resolution order for the secrets file:
  1. $PODMIND_SECRETS (explicit path)
  2. ~/.config/podmind/secrets.json
  3. ~/.config/podcast-wiki/secrets.json   (legacy vaults)

File is JSON, mode 0600. Missing file loads as {} so callers can give
field-specific errors.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def secrets_path() -> Path:
    env = os.environ.get("PODMIND_SECRETS")
    if env:
        return Path(env).expanduser()
    new = Path.home() / ".config/podmind/secrets.json"
    if new.exists():
        return new
    legacy = Path.home() / ".config/podcast-wiki/secrets.json"
    if legacy.exists():
        return legacy
    return new


def load() -> dict[str, str]:
    p = secrets_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def get(key: str) -> str | None:
    return load().get(key)


def save(data: dict[str, str]) -> None:
    p = secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def update(**kwargs: str) -> None:
    cur = load()
    cur.update(kwargs)
    save(cur)
