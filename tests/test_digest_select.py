"""Tests for podmind.digest_select.merge_episode_sources (pure, no I/O)."""
from __future__ import annotations

from podmind.frontmatter import EpisodePage
from podmind.digest_select import merge_episode_sources, is_engaged


def _ep(*, listened=False, played_up_to=0, duration_min=30) -> EpisodePage:
    return EpisodePage(raw_dir="x/y", date="2026-06-17", show="S",
                       listened=listened, played_up_to=played_up_to,
                       duration_min=duration_min, guests=[],
                       transcript_source="x", body="h", hook="h")


def test_is_engaged_listened_true():
    assert is_engaged(_ep(listened=True, played_up_to=0)) is True  # e.g. YouTube


def test_is_engaged_below_50_percent_excluded():
    # 4 seconds of a 17-min episode → ~0.4%, excluded
    assert is_engaged(_ep(played_up_to=4, duration_min=17)) is False
    # 40% played → still excluded
    assert is_engaged(_ep(played_up_to=int(0.4 * 17 * 60), duration_min=17)) is False


def test_is_engaged_at_or_above_50_percent_included():
    assert is_engaged(_ep(played_up_to=int(0.5 * 17 * 60), duration_min=17)) is True
    assert is_engaged(_ep(played_up_to=int(0.8 * 30 * 60), duration_min=30)) is True


def test_is_engaged_unknown_duration_needs_listened():
    # No duration to judge a fraction → only `listened` counts
    assert is_engaged(_ep(played_up_to=600, duration_min=0)) is False
    assert is_engaged(_ep(listened=True, played_up_to=600, duration_min=0)) is True


def make_ep(raw_dir, date="2026-06-17") -> EpisodePage:
    return EpisodePage(
        raw_dir=raw_dir, date=date, show="S", listened=True,
        played_up_to=0, duration_min=30, guests=[],
        transcript_source="x", body="h", hook="h",
    )


def test_dedupe_by_raw_dir_primary_wins():
    primary = [make_ep("a", "2026-06-10")]
    secondary = [make_ep("a", "2026-06-17"), make_ep("b", "2026-06-16")]
    kept, dropped = merge_episode_sources(primary, secondary, max_n=10)
    rds = [e.raw_dir for e in kept]
    assert rds.count("a") == 1
    a = next(e for e in kept if e.raw_dir == "a")
    assert a.date == "2026-06-10"  # the primary 'a', not secondary's dup
    assert "b" in rds
    assert dropped == 0


def test_secondary_youtube_included():
    primary = [make_ep("podcast/x")]
    secondary = [make_ep("yt-veritasium/vid")]
    kept, _ = merge_episode_sources(primary, secondary, max_n=10)
    assert any(e.raw_dir == "yt-veritasium/vid" for e in kept)


def test_sorted_by_date_desc():
    primary = [make_ep("a", "2026-06-10")]
    secondary = [make_ep("b", "2026-06-17"), make_ep("c", "2026-06-01")]
    kept, _ = merge_episode_sources(primary, secondary, max_n=10)
    assert [e.date for e in kept] == ["2026-06-17", "2026-06-10", "2026-06-01"]


def test_cap_and_dropped_count():
    primary = [make_ep(f"p{i}", f"2026-06-{10 + i:02d}") for i in range(3)]
    secondary = [make_ep(f"s{i}", f"2026-05-{10 + i:02d}") for i in range(4)]
    kept, dropped = merge_episode_sources(primary, secondary, max_n=5)
    assert len(kept) == 5
    assert dropped == 2  # 7 total - 5
    assert all(e.raw_dir.startswith("p") for e in kept[:3])  # June primaries on top


def test_empty_primary_uses_secondary():
    secondary = [make_ep("yt/a"), make_ep("yt/b")]
    kept, dropped = merge_episode_sources([], secondary, max_n=10)
    assert len(kept) == 2
    assert dropped == 0


def test_empty_both():
    assert merge_episode_sources([], [], max_n=10) == ([], 0)


def test_episode_without_raw_dir_is_kept():
    primary = [make_ep("a")]
    secondary = [make_ep(None, "2026-06-18"), make_ep("a", "2026-06-18")]
    kept, _ = merge_episode_sources(primary, secondary, max_n=10)
    assert sum(1 for e in kept if e.raw_dir is None) == 1   # no key → not deduped
    assert sum(1 for e in kept if e.raw_dir == "a") == 1    # dup 'a' skipped
