"""Tests for podmind.digest_email — structured digest → email HTML.

Pure functions only. A fake `meta_loader` stands in for meta.json reads, so
no test touches disk or network (except the explicit _default_meta_loader
tests, which use tmp_path + monkeypatch).
"""
from __future__ import annotations

import json

import pytest

from podmind.frontmatter import EpisodePage
from podmind.threads import Thread
from podmind import paths, digest_email
from podmind.digest_email import (
    EpisodeMeta,
    _vault_link,
    _slug_to_title,
    _default_meta_loader,
    _hook_text,
    render_email_html,
)


def make_ep(*, show="Some Show", duration_min=30, played_up_to=0,
            listened=False, hook="A hook sentence.", raw_dir="x/y") -> EpisodePage:
    return EpisodePage(
        raw_dir=raw_dir, date="2026-06-17", show=show, listened=listened,
        played_up_to=played_up_to, duration_min=duration_min, guests=[],
        transcript_source="whisper", body=hook, hook=hook,
    )


def fake_loader(table: dict[str, EpisodeMeta]):
    def _loader(slug: str, ep):
        return table.get(slug, EpisodeMeta(_slug_to_title(slug), None))
    return _loader


def test_slug_to_title_dekebabs():
    assert _slug_to_title("some-cool-episode") == "Some cool episode"


def test_vault_link_encoding():
    link = _vault_link("ep-a", "podcast wiki")
    assert link == "obsidian://open?vault=podcast%20wiki&file=wiki/episodes/ep-a"


def test_default_loader_prefers_youtube(tmp_path, monkeypatch):
    d = tmp_path / "raw" / "episodes" / "show" / "ep"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "title": "Real Title",
        "youtube_url": "https://yt/1",
        "audio_url": "https://audio/1",
    }))
    monkeypatch.setattr(paths, "RAW_DIR", tmp_path / "raw")
    meta = _default_meta_loader("ep", make_ep(raw_dir="show/ep"))
    assert meta.title == "Real Title"
    assert meta.source_url == "https://yt/1"


def test_default_loader_audio_fallback(tmp_path, monkeypatch):
    d = tmp_path / "raw" / "episodes" / "show" / "ep"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "title": "Audio Only", "audio_url": "https://audio/2",
    }))
    monkeypatch.setattr(paths, "RAW_DIR", tmp_path / "raw")
    meta = _default_meta_loader("ep", make_ep(raw_dir="show/ep"))
    assert meta.source_url == "https://audio/2"


def test_default_loader_missing_meta_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "RAW_DIR", tmp_path / "raw")
    meta = _default_meta_loader("some-cool-episode", make_ep(raw_dir="nope/missing"))
    assert meta.source_url is None
    assert meta.title == "Some cool episode"


def test_hook_text_strips_badge_and_show_prefix():
    ep = make_ep(hook="🎧 [[shows/real-vision]] — Oil markets and the Iran deal.")
    assert _hook_text(ep) == "Oil markets and the Iran deal."


def test_hook_text_passthrough_without_prefix():
    ep = make_ep(hook="Just a plain hook sentence.")
    assert _hook_text(ep) == "Just a plain hook sentence."


def test_title_links_to_source_and_no_raw_slug():
    eps = {"ep-a": make_ep()}
    loader = fake_loader({"ep-a": EpisodeMeta("Episode A Title", "https://youtu.be/abc")})
    html = render_email_html([], ["ep-a"], date_str="2026-06-17",
                             episode_lookup=eps, meta_loader=loader)
    assert "Episode A Title" in html
    assert 'href="https://youtu.be/abc"' in html
    assert "[[episodes" not in html


def test_no_source_url_renders_unlinked_title():
    eps = {"ep-a": make_ep()}
    loader = fake_loader({"ep-a": EpisodeMeta("Plain Title", None)})
    html = render_email_html([], ["ep-a"], date_str="2026-06-17",
                             episode_lookup=eps, meta_loader=loader)
    assert "Plain Title" in html
    assert '<a href="http' not in html  # source link absent (vault link is obsidian://)


def test_vault_deeplink_present_and_escaped():
    eps = {"ep-a": make_ep()}
    html = render_email_html([], ["ep-a"], date_str="2026-06-17",
                             episode_lookup=eps, meta_loader=fake_loader({}))
    assert "obsidian://open?vault=podcast-wiki&amp;file=wiki/episodes/ep-a" in html


def test_badge_listened():
    eps = {"a": make_ep(listened=True)}
    html = render_email_html([], ["a"], date_str="d", episode_lookup=eps,
                             meta_loader=fake_loader({}))
    assert "🎧" in html


def test_badge_in_progress_percent():
    eps = {"a": make_ep(listened=False, played_up_to=900, duration_min=30)}  # 900/1800 = 50%
    html = render_email_html([], ["a"], date_str="d", episode_lookup=eps,
                             meta_loader=fake_loader({}))
    assert "▶ 50%" in html


def test_badge_unplayed():
    eps = {"a": make_ep(listened=False, played_up_to=0)}
    html = render_email_html([], ["a"], date_str="d", episode_lookup=eps,
                             meta_loader=fake_loader({}))
    assert "⚪" in html


def test_html_escaping():
    eps = {"a": make_ep(show="Tom & Jerry", hook="1 < 2 & 3 > 0")}
    loader = fake_loader({"a": EpisodeMeta("Title & <b>", None)})
    html = render_email_html([], ["a"], date_str="d", episode_lookup=eps,
                             meta_loader=loader)
    assert "Title &amp; &lt;b&gt;" in html
    assert "Tom &amp; Jerry" in html
    assert "1 &lt; 2 &amp; 3 &gt; 0" in html


def test_threads_and_uncategorized_structure():
    eps = {s: make_ep() for s in ["a", "b", "c"]}
    t = Thread(name="Big Story", summary="why these belong.", episode_slugs=["a", "b"])
    html = render_email_html([t], ["c"], date_str="2026-06-17",
                             episode_lookup=eps, meta_loader=fake_loader({}))
    assert "Big Story (2 episodes)" in html
    assert "why these belong." in html
    assert "Uncategorized · 1" in html
    assert "3 episodes across 1 thread" in html


def test_empty_digest_renders():
    html = render_email_html([], [], date_str="2026-06-17", episode_lookup={},
                             meta_loader=fake_loader({}))
    assert "0 episodes across 0 threads" in html


def test_source_override_wins_over_meta_source():
    eps = {"ep-a": make_ep()}
    loader = fake_loader({"ep-a": EpisodeMeta("T", "https://audio/x.mp3")})
    html = render_email_html([], ["ep-a"], date_str="d", episode_lookup=eps,
                             meta_loader=loader,
                             source_overrides={"ep-a": "https://pca.st/episode/abc"})
    assert 'href="https://pca.st/episode/abc"' in html
    assert "audio/x.mp3" not in html


def test_source_override_absent_falls_back_to_meta():
    eps = {"ep-a": make_ep()}
    loader = fake_loader({"ep-a": EpisodeMeta("T", "https://yt/1")})
    html = render_email_html([], ["ep-a"], date_str="d", episode_lookup=eps,
                             meta_loader=loader, source_overrides={})
    assert 'href="https://yt/1"' in html


def test_source_overrides_none_arg_is_safe():
    eps = {"ep-a": make_ep()}
    loader = fake_loader({"ep-a": EpisodeMeta("T", "https://yt/1")})
    html = render_email_html([], ["ep-a"], date_str="d", episode_lookup=eps,
                             meta_loader=loader)  # no source_overrides kwarg
    assert 'href="https://yt/1"' in html


def test_youtube_slug_renders_play_badge_not_headphones():
    eps = {"yt-veritasium-x": make_ep(listened=True)}
    html = render_email_html([], ["yt-veritasium-x"], date_str="d",
                             episode_lookup=eps, meta_loader=fake_loader({}))
    assert "▶️" in html        # red play button for YouTube
    assert "🎧" not in html     # not the podcast listened badge


def test_podcast_slug_keeps_headphones_badge():
    eps = {"real-vision-x": make_ep(listened=True)}
    html = render_email_html([], ["real-vision-x"], date_str="d",
                             episode_lookup=eps, meta_loader=fake_loader({}))
    assert "🎧" in html
    assert "▶️" not in html


def test_in_progress_podcast_unaffected_by_youtube_badge():
    eps = {"real-vision-y": make_ep(listened=False, played_up_to=900, duration_min=30)}
    html = render_email_html([], ["real-vision-y"], date_str="d",
                             episode_lookup=eps, meta_loader=fake_loader({}))
    assert "▶ 50%" in html      # plain in-progress badge, distinct from ▶️
    assert "▶️" not in html
