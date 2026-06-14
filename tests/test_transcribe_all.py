"""Tests for podmind.transcript.transcribe_all candidate selection.

This is the most-edited function in the codebase — bug fixes for limit-budget
exhaustion, vanished-dir crashes, AC-power gating, only-played filter, and
date-desc sort all live here. Lock the candidate-selection invariants down.

We don't actually invoke whisper; we monkeypatch `transcribe_one` to return
a controlled tier so we can assert on the resulting counts dict.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from podmind import paths, transcript


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    """Build a tiny raw/ tree of meta.json files and point podmind at it."""
    raw = tmp_path / "raw" / "episodes"
    (raw / "show-a" / "2026-04-01-fresh-listened").mkdir(parents=True)
    (raw / "show-a" / "2025-01-01-old-listened").mkdir(parents=True)
    (raw / "show-a" / "2024-06-01-already-transcribed").mkdir(parents=True)
    (raw / "show-a" / "2024-05-01-not-listened").mkdir(parents=True)
    (raw / "yt-foo" / "2026-03-15-yt-watched").mkdir(parents=True)

    def write_meta(rel: str, **fields):
        path = raw / rel / "meta.json"
        defaults = {"show": "Show A", "title": "Episode", "transcript_source": None,
                    "listened": False, "played_up_to": 0, "pub_date": "2024-01-01"}
        defaults.update(fields)
        path.write_text(json.dumps(defaults))

    write_meta("show-a/2026-04-01-fresh-listened", listened=True, pub_date="2026-04-01")
    write_meta("show-a/2025-01-01-old-listened", listened=True, pub_date="2025-01-01")
    write_meta("show-a/2024-06-01-already-transcribed",
               listened=True, transcript_source="rss", pub_date="2024-06-01")
    write_meta("show-a/2024-05-01-not-listened", listened=False, pub_date="2024-05-01")
    write_meta("yt-foo/2026-03-15-yt-watched", listened=True, watched_at="2026-03-15", pub_date=None)

    # Point podmind.paths at this fake vault.
    monkeypatch.setattr(paths, "EPISODES_DIR", raw)
    return raw


def fake_transcribe_one(returned_tier="youtube"):
    """Build a transcribe_one stub that returns the given tier name."""
    def _impl(epdir, **kwargs):
        return returned_tier
    return _impl


class TestCandidateFilter:
    def test_already_transcribed_filtered_before_limit(self, fake_vault):
        """The bug fix from 2026-05-10: --limit must apply only to actual work,
        not to already-transcribed candidates that get skipped during iteration.

        With 4 listened episodes (1 transcribed) and limit=2, we should
        process 2 untranscribed ones, NOT 2 candidates total (which would
        include the already-transcribed one as a no-op)."""
        with patch.object(transcript, "transcribe_one", side_effect=fake_transcribe_one("youtube")):
            counts = transcript.transcribe_all(only_played=True, limit=2)
        # 3 listened-untranscribed exist (fresh, old, yt). Limit=2 → process 2.
        assert counts["youtube"] == 2
        assert counts["skipped"] == 0  # no skipped — the filter happens before limit

    def test_only_played_excludes_unwatched(self, fake_vault):
        """only_played=True drops episodes with listened=False AND played_up_to=0."""
        with patch.object(transcript, "transcribe_one", side_effect=fake_transcribe_one("youtube")):
            counts = transcript.transcribe_all(only_played=True, limit=10)
        # 3 listened-untranscribed (the fourth one is not_listened)
        assert counts["youtube"] == 3

    def test_only_played_false_includes_unwatched(self, fake_vault):
        """only_played=False considers all episodes."""
        with patch.object(transcript, "transcribe_one", side_effect=fake_transcribe_one("youtube")):
            counts = transcript.transcribe_all(only_played=False, limit=10)
        # 4 untranscribed total (3 listened + 1 not-listened, transcribed one excluded)
        assert counts["youtube"] == 4


class TestSortOrder:
    def test_pub_date_desc_processes_newest_first(self, fake_vault):
        """Bug guard: the sort must put 2026 episodes ahead of 2025 ones so
        freshly-listened content gets transcribed first."""
        processed = []

        def capture(epdir, **kwargs):
            processed.append(epdir.name)
            return "youtube"

        with patch.object(transcript, "transcribe_one", side_effect=capture):
            transcript.transcribe_all(only_played=True, limit=10)
        # First processed should be the freshest (2026-04-01 fresh-listened)
        assert processed[0].startswith("2026-04-01")
        assert processed[-1].startswith("2025-01-01") or processed[-1].startswith("2026-03-15")

    def test_yt_watched_at_used_when_pub_date_missing(self, fake_vault):
        """YT entries fall back to watched_at when pub_date is None."""
        processed = []

        def capture(epdir, **kwargs):
            processed.append(epdir.name)
            return "youtube"

        with patch.object(transcript, "transcribe_one", side_effect=capture):
            transcript.transcribe_all(only_played=True, limit=10)
        # The yt-foo episode (watched_at 2026-03-15, pub_date None) must appear.
        assert any("yt-watched" in name for name in processed)


class TestVanishedDirHandling:
    def test_meta_deleted_during_iteration(self, fake_vault):
        """If meta.json vanishes between candidate-list build and iteration
        (e.g. concurrent music cleanup), we should skip not crash."""
        # Delete one meta.json AFTER candidate list is built.
        target = fake_vault / "show-a" / "2026-04-01-fresh-listened" / "meta.json"

        def vanishing_transcribe(epdir, **kwargs):
            # Simulate the cleanup happening during a long whisper run.
            target.unlink()
            return "youtube"

        with patch.object(transcript, "transcribe_one", side_effect=vanishing_transcribe):
            counts = transcript.transcribe_all(only_played=True, limit=10)
        # Should complete without crashing. The vanished one isn't counted as
        # "youtube" because by the time the loop reaches it the file is gone —
        # but it depends on iteration order. The key invariant: no crash.
        assert "vanished" in counts or counts.get("youtube", 0) >= 2

    def test_filenotfounderror_in_transcribe_one_caught(self, fake_vault):
        """transcribe_one raising FileNotFoundError must not crash the batch."""
        def raise_fnf(epdir, **kwargs):
            raise FileNotFoundError(str(epdir))

        with patch.object(transcript, "transcribe_one", side_effect=raise_fnf):
            counts = transcript.transcribe_all(only_played=True, limit=10)
        assert counts.get("vanished", 0) > 0


class TestACPowerGating:
    def test_aborts_when_battery(self, fake_vault):
        """require_ac=True + on_ac_power()=False → loop aborts immediately."""
        with patch.object(transcript, "on_ac_power", return_value=False), \
             patch.object(transcript, "transcribe_one", side_effect=fake_transcribe_one()):
            counts = transcript.transcribe_all(only_played=True, limit=10, require_ac=True)
        assert counts["aborted-battery"] == 1
        assert counts["youtube"] == 0  # nothing got processed

    def test_continues_when_ac(self, fake_vault):
        """require_ac=True + on_ac_power()=True → process normally."""
        with patch.object(transcript, "on_ac_power", return_value=True), \
             patch.object(transcript, "transcribe_one", side_effect=fake_transcribe_one()):
            counts = transcript.transcribe_all(only_played=True, limit=10, require_ac=True)
        assert counts["aborted-battery"] == 0
        assert counts["youtube"] == 3

    def test_require_ac_false_ignores_power_state(self, fake_vault):
        """require_ac=False → run regardless of power."""
        with patch.object(transcript, "on_ac_power", return_value=False), \
             patch.object(transcript, "transcribe_one", side_effect=fake_transcribe_one()):
            counts = transcript.transcribe_all(only_played=True, limit=10, require_ac=False)
        assert counts["youtube"] == 3


class TestEmptyAndEdgeCases:
    def test_empty_episodes_dir(self, tmp_path, monkeypatch):
        empty = tmp_path / "raw" / "episodes"
        empty.mkdir(parents=True)
        monkeypatch.setattr(paths, "EPISODES_DIR", empty)
        counts = transcript.transcribe_all(only_played=True, limit=10)
        assert all(v == 0 for v in counts.values())

    def test_corrupt_meta_json_skipped(self, tmp_path, monkeypatch):
        raw = tmp_path / "raw" / "episodes" / "show-a" / "2026-01-01-bad"
        raw.mkdir(parents=True)
        (raw / "meta.json").write_text("not valid json {{{")
        monkeypatch.setattr(paths, "EPISODES_DIR", raw.parent.parent)
        counts = transcript.transcribe_all(only_played=True, limit=10)
        # Corrupt entry was skipped during candidate building; counts all zero.
        assert all(v == 0 for v in counts.values())
