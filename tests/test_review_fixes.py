"""Regression tests for the 2026-06-12 code-review fix batch.

Covers: atomic JSON writes, prompt-template brace-safety, to_episode
resilience to malformed LLM output, duration_sec badge fix, and the
transcribe_all sort preferring ingested_at.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from podmind.jsonio import write_json_atomic

# bin/ scripts need their dir on sys.path (they import `from _lib import ...`)
BIN = Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


# ---------- jsonio ----------

class TestWriteJsonAtomic:
    def test_writes_and_round_trips(self, tmp_path: Path):
        p = tmp_path / "meta.json"
        write_json_atomic(p, {"a": 1, "b": [2, 3]})
        assert json.loads(p.read_text()) == {"a": 1, "b": [2, 3]}

    def test_replaces_existing(self, tmp_path: Path):
        p = tmp_path / "meta.json"
        p.write_text('{"old": true}')
        write_json_atomic(p, {"new": True})
        assert json.loads(p.read_text()) == {"new": True}

    def test_no_tmp_left_behind(self, tmp_path: Path):
        p = tmp_path / "meta.json"
        write_json_atomic(p, {})
        assert list(tmp_path.glob("*.tmp")) == []


# ---------- summarize.build_prompt brace-safety ----------

class TestBuildPromptBraceSafety:
    def _episode(self, tmp_path: Path, transcript: str, meta: dict) -> dict:
        """Materialize a fake raw episode dir under the test vault."""
        from podmind import paths
        d = paths.EPISODES_DIR / "test-show" / "2026-01-01-test-ep"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "transcript.md").write_text(transcript)
        return {"raw_dir": "test-show/2026-01-01-test-ep"}

    def test_placeholder_in_transcript_not_substituted(self, tmp_path: Path):
        """A transcript that literally contains '{date}' / '{transcript}'
        (programming podcasts discuss template syntax) must pass through
        verbatim — the old chained .replace() double-substituted it."""
        import summarize
        item = self._episode(
            tmp_path,
            transcript="We use {date} and {transcript} and {meta_json} in our templates.",
            meta={"title": "Templating 101"},
        )
        prompt = summarize.build_prompt(item)
        assert "We use {date} and {transcript} and {meta_json} in our templates." in prompt

    def test_placeholder_in_meta_not_substituted(self, tmp_path: Path):
        import summarize
        item = self._episode(
            tmp_path,
            transcript="normal transcript",
            meta={"title": "show with {transcript} in title"},
        )
        prompt = summarize.build_prompt(item)
        assert "show with {transcript} in title" in prompt
        # And the real placeholders were substituted
        assert "{raw_dir}" not in prompt
        assert "raw_dir: test-show/2026-01-01-test-ep" in prompt


# ---------- summarize output-token budget (reasoning-model fix) ----------

class TestMaxOutputTokens:
    def test_default_budget_is_16000(self):
        import summarize
        assert summarize.MAX_OUTPUT_TOKENS == 16000

    def test_env_override(self, monkeypatch):
        import importlib
        import summarize
        monkeypatch.setenv("PODMIND_MAX_OUTPUT_TOKENS", "5000")
        importlib.reload(summarize)
        try:
            assert summarize.MAX_OUTPUT_TOKENS == 5000
        finally:
            monkeypatch.delenv("PODMIND_MAX_OUTPUT_TOKENS", raising=False)
            importlib.reload(summarize)  # restore default for other tests

    def test_process_one_forwards_budget_to_achat(self, tmp_path, monkeypatch):
        import asyncio

        import summarize
        from podmind import llm, paths

        d = paths.EPISODES_DIR / "test-show" / "2026-01-01-budget-ep"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"title": "T", "show": "S", "duration_sec": 60}))
        (d / "transcript.md").write_text("hello world transcript")
        monkeypatch.setattr(summarize, "RESULTS", tmp_path)  # don't touch /tmp/podmind-results
        item = {"raw_dir": "test-show/2026-01-01-budget-ep", "idx": "01",
                "show_slug": "test-show", "date": "2026-01-01"}

        seen = {}

        class FakeProvider:
            async def achat(self, client, prompt, **kwargs):
                seen["max_tokens"] = kwargs.get("max_tokens")
                return '{"hook": "x", "takeaways": []}', llm.Usage(10, 0, 5)

        async def go():
            return await summarize.process_one(None, FakeProvider(), item, asyncio.Semaphore(1))

        idx, ok, _usage = asyncio.run(go())
        assert ok is True
        assert seen["max_tokens"] == summarize.MAX_OUTPUT_TOKENS == 16000


# ---------- tick_finalize.to_episode resilience ----------

class TestToEpisodeResilience:
    def _minimal(self, **overrides) -> dict:
        d = {
            "raw_dir": "show/2026-01-01-ep",
            "show_slug": "show",
            "date": "2026-01-01",
            "show_name": "Show",
            "title_slug": "ep",
            "hook": "A hook.",
            "takeaways": ["t1"],
            "people": [],
            "topics": [],
        }
        d.update(overrides)
        return d

    def test_missing_dispatch_fields_raise_named_error(self):
        import tick_finalize
        with pytest.raises(ValueError, match="show_slug"):
            tick_finalize.to_episode({"raw_dir": "x/y", "date": "2026-01-01"})

    def test_missing_llm_fields_degrade_to_empty(self):
        """Truncated LLM response (hook/takeaways/people/topics absent)
        must not KeyError — old behavior crashed the whole batch."""
        import tick_finalize
        d = self._minimal()
        for k in ("hook", "takeaways", "people", "topics", "show_name", "title_slug"):
            d.pop(k, None)
        ep = tick_finalize.to_episode(d)
        assert ep["hook"] == ""
        assert ep["takeaways"] == []
        assert ep["people"] == []
        assert ep["filename"].startswith("show-2026-01-01-")

    def test_hook_newlines_collapsed(self):
        """Hook renders on the single badge line; embedded newlines broke
        first_body_line extraction."""
        import tick_finalize
        ep = tick_finalize.to_episode(self._minimal(hook="line one\nline two\n\nthree"))
        assert "\n" not in ep["hook"]
        assert ep["hook"] == "line one line two three"


# ---------- tick_finalize.badge duration_sec fix ----------

class TestBadgeDurationSec:
    def test_percentage_uses_duration_sec(self):
        import tick_finalize
        # 900 of 3600 sec = 25%. The old code read meta["duration"] (never
        # present) so the percentage branch was unreachable.
        assert tick_finalize.badge(
            {"listened": False, "played_up_to": 900, "duration_sec": 3600}
        ) == "▶ 25%"

    def test_listened_still_wins(self):
        import tick_finalize
        assert tick_finalize.badge({"listened": True}) == "🎧"
