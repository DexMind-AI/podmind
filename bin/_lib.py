"""Shared helpers for bin/ scripts.

Path/secret resolution is centralized here; scripts re-export from this module
rather than re-deriving paths or hard-coding home-relative locations.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running scripts directly from a checkout without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from podmind import paths as _paths  # noqa: E402
from podmind import secrets as _secrets  # noqa: E402
from podmind.curation import is_music_channel  # noqa: E402, F401  (re-exported)

EPISODES_DIR = _paths.EPISODES_DIR
WIKI_DIR = _paths.WIKI_DIR
LOG_FILE = _paths.LOG_FILE
DATA_ROOT = _paths.DATA_ROOT

SECRETS = _secrets.secrets_path()


def load_secret(key: str) -> str:
    val = _secrets.get(key)
    if val is None:
        raise KeyError(f"{key!r} not found in {SECRETS}")
    return val


def parse_raw_dir(ep_path: Path) -> str | None:
    """Read the `raw_dir:` value from a wiki episode page's YAML frontmatter.

    Thin wrapper around :func:`podmind.frontmatter.read_raw_dir` kept here for
    bin-script ergonomics; new code should import from the package directly.
    """
    from podmind.frontmatter import read_raw_dir
    return read_raw_dir(ep_path)
