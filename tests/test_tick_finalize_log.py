"""Regression test: tick_finalize.main() must not crash on a fresh vault with no wiki/log.md.

Covers the fix at bin/tick_finalize.py where log.read_text() raised FileNotFoundError
on first run against a vault that had never been ingested.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

DEMO_VAULT = Path(__file__).resolve().parent.parent / "examples" / "demo-vault"
PREBAKED = DEMO_VAULT / "prebaked-results"


@pytest.fixture()
def fresh_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A minimal vault with raw episodes but NO wiki/log.md."""
    vault = tmp_path / "vault"
    raw_src = DEMO_VAULT / "raw"
    shutil.copytree(raw_src, vault / "raw")

    wiki = vault / "wiki"
    for d in ("episodes", "people", "topics", "shows", "synthesis"):
        (wiki / d).mkdir(parents=True)
    # Intentionally do NOT create wiki/log.md — that's the regression scenario.

    monkeypatch.setenv("PODMIND_DATA_ROOT", str(vault))

    results = tmp_path / "results"
    results.mkdir()
    for src in PREBAKED.glob("*.json"):
        shutil.copy(src, results / src.name)

    return vault, results


def test_finalize_creates_log_on_fresh_vault(
    fresh_vault: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    """main() must succeed and create wiki/log.md when the file doesn't exist."""
    vault, results = fresh_vault
    log_path = vault / "wiki" / "log.md"
    assert not log_path.exists(), "precondition: log.md must not exist"

    # Patch the module-level constants BEFORE importing (they're resolved at
    # import time from PODMIND_DATA_ROOT which we've already monkeypatched).
    import importlib
    import tick_finalize as tf

    # Force the module to use our vault's paths.
    monkeypatch.setattr(tf, "WIKI", vault / "wiki")
    monkeypatch.setattr(tf, "RAW_EP", vault / "raw" / "episodes")
    monkeypatch.setattr(tf, "RESULTS", results)

    monkeypatch.setattr(sys, "argv", ["tick_finalize.py", "1", "--note", "fresh-vault test"])
    tf.main()

    assert log_path.exists(), "wiki/log.md must be created by finalize on first run"
    content = log_path.read_text()
    assert "## [" in content, "log must contain a dated ingest entry"
    assert "ingest" in content
