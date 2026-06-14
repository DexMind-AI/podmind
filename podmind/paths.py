"""Canonical paths and slug helpers.

The data root (where `raw/` and `wiki/` live) is decoupled from the package
location via the `PODMIND_DATA_ROOT` env var. Set it to the vault directory
that contains your `raw/` and `wiki/` subdirectories.

Resolution order:
  1. `PODMIND_DATA_ROOT` env var (preferred — set in the vault's .envrc)
  2. Two-levels-up from this file (legacy: `~/podcast-wiki/podcast_wiki/paths.py`
     style where the package sits inside the vault)
  3. Current working directory (last resort)
"""
from __future__ import annotations

import os
from pathlib import Path

from slugify import slugify


def _resolve_data_root() -> Path:
    env = os.environ.get("PODMIND_DATA_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p
    # Legacy fallback: package directly inside the vault
    legacy = Path(__file__).resolve().parents[1]
    if (legacy / "raw").exists() or (legacy / "wiki").exists():
        return legacy
    # Last resort: cwd
    return Path.cwd().resolve()


DATA_ROOT = _resolve_data_root()
RAW_DIR = DATA_ROOT / "raw"
FEEDS_DIR = RAW_DIR / "feeds"
EPISODES_DIR = RAW_DIR / "episodes"
AUDIO_DIR = RAW_DIR / "audio"
WIKI_DIR = DATA_ROOT / "wiki"
LOG_FILE = WIKI_DIR / "log.md"

# Backwards-compat alias — some scripts may still reference REPO_ROOT.
REPO_ROOT = DATA_ROOT


def show_slug(title: str) -> str:
    return slugify(title, max_length=60)


def episode_slug(pub_date: str, title: str) -> str:
    """`pub_date` is ISO YYYY-MM-DD."""
    return f"{pub_date}-{slugify(title, max_length=80)}"


def episode_dir(show_title: str, pub_date: str, ep_title: str) -> Path:
    return EPISODES_DIR / show_slug(show_title) / episode_slug(pub_date, ep_title)
