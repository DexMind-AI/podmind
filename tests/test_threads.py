"""Tests for podmind.threads (pure-data) and podmind.threads_llm (LLM parsing).

Real LLM calls aren't exercised in unit tests — we inject a fake `chat_fn`
into `synthesize_threads` to test parsing + fallback logic offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from podmind import llm, paths
from podmind.frontmatter import EpisodePage
from podmind.threads import (
    Thread,
    bridges_in_window,
    extract_links,
    format_threads_md,
)
from podmind.threads_llm import (
    parse_llm_response,
    synthesize_threads,
    key_takeaways,
    format_episodes_for_prompt,
    _extract_json,
)


# ---------- Vault fixtures ----------

def _write_episode(slug: str, body: str, *, listened: bool = True,
                   played_up_to: int = 0, date: str = "2026-05-10") -> Path:
    eps_dir = paths.WIKI_DIR / "episodes"
    eps_dir.mkdir(parents=True, exist_ok=True)
    p = eps_dir / f"{slug}.md"
    frontmatter = (
        f"---\n"
        f"raw_dir: somewhere/{slug}\n"
        f"date: {date}\n"
        f"listened: {str(listened).lower()}\n"
        f"played_up_to: {played_up_to}\n"
        f"---\n\n"
    )
    p.write_text(frontmatter + body)
    return p


def _write_person(slug: str) -> Path:
    d = paths.WIKI_DIR / "people"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(f"# {slug}\n")
    return d / f"{slug}.md"


def _write_topic(slug: str) -> Path:
    d = paths.WIKI_DIR / "topics"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(f"# {slug}\n")
    return d / f"{slug}.md"


@pytest.fixture(autouse=True)
def _clean_vault():
    for sub in ("episodes", "people", "topics", "threads"):
        d = paths.WIKI_DIR / sub
        if d.exists():
            for p in d.glob("*"):
                if p.is_file():
                    p.unlink()
    yield


# ---------- extract_links / existing_targets ----------

class TestExtractLinks:
    def test_basic(self):
        ep = _write_episode("ep1", "[[people/x]] [[topics/y]]")
        people, topics = extract_links(ep)
        assert people == {"x"}
        assert topics == {"y"}

    def test_alias_form(self):
        ep = _write_episode("ep1", "[[people/x|Mr X]] [[topics/y|the Y thing]]")
        people, topics = extract_links(ep)
        assert people == {"x"} and topics == {"y"}

    def test_placeholder_skipped(self):
        ep = _write_episode("ep1", "[[people/...]] [[people/<unknown>]]")
        people, _ = extract_links(ep)
        assert people == set()


# ---------- bridges_in_window ----------

class TestBridgesInWindow:
    def test_single_cite_not_a_bridge(self):
        _write_person("alice")
        ep = _write_episode("ep1", "[[people/alice]]")
        p_br, t_br = bridges_in_window([ep])
        assert p_br == {} and t_br == {}

    def test_two_cites_is_a_bridge(self):
        _write_person("alice")
        ep1 = _write_episode("ep1", "[[people/alice]]")
        ep2 = _write_episode("ep2", "[[people/alice]]")
        p_br, _ = bridges_in_window([ep1, ep2])
        assert p_br == {"alice": ["ep1", "ep2"]}

    def test_dangling_filtered(self):
        # alice has no page → filtered, even though referenced twice
        ep1 = _write_episode("ep1", "[[people/alice]]")
        ep2 = _write_episode("ep2", "[[people/alice]]")
        p_br, _ = bridges_in_window([ep1, ep2])
        assert p_br == {}

    def test_min_degree_param(self):
        _write_person("alice")
        ep1 = _write_episode("ep1", "[[people/alice]]")
        ep2 = _write_episode("ep2", "[[people/alice]]")
        ep3 = _write_episode("ep3", "[[people/alice]]")
        # min_degree=3 means alice must appear in ≥3 episodes
        p_br, _ = bridges_in_window([ep1, ep2], min_degree=3)
        assert p_br == {}
        p_br, _ = bridges_in_window([ep1, ep2, ep3], min_degree=3)
        assert "alice" in p_br


# ---------- Thread dataclass ----------

class TestThread:
    def test_construct_ok(self):
        t = Thread(name="X", summary="Y", episode_slugs=["a", "b"])
        assert t.name == "X"

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            Thread(name="  ", summary="x", episode_slugs=["a"])

    def test_no_episodes_rejected(self):
        with pytest.raises(ValueError, match="no episodes"):
            Thread(name="X", summary="y", episode_slugs=[])


# ---------- format_threads_md ----------

class TestFormatThreadsMd:
    def test_basic_structure(self):
        threads = [
            Thread(name="Big thread", summary="Through-line.",
                   episode_slugs=["ep1", "ep2", "ep3"]),
            Thread(name="Small thread", summary="Smaller.",
                   episode_slugs=["ep4", "ep5"]),
        ]
        md = format_threads_md(threads, ["ep6"], date_str="2026-05-14")
        # Header
        assert "# Threads — 2026-05-14" in md
        # Threads sorted by episode count (Big first)
        assert md.index("Big thread") < md.index("Small thread")
        # Episode counts in headings
        assert "Big thread (3 episodes)" in md
        assert "Small thread (2 episodes)" in md
        # Wiki-link format
        assert "[[episodes/ep1]]" in md
        # Uncategorized section
        assert "Uncategorized (1)" in md
        assert "[[episodes/ep6]]" in md

    def test_no_uncategorized_when_empty(self):
        threads = [Thread(name="X", summary="Y", episode_slugs=["a", "b"])]
        md = format_threads_md(threads, [], date_str="2026-05-14")
        assert "Uncategorized" not in md

    def test_summary_line_pluralizes(self):
        one_thread = format_threads_md(
            [Thread(name="X", summary="Y", episode_slugs=["a", "b"])],
            [], date_str="2026-05-14")
        assert "2 episodes across 1 thread in" in one_thread   # "1 thread", not "1 threads"
        one_ep = format_threads_md([], ["a"], date_str="2026-05-14")
        assert "1 episode across 0 threads in" in one_ep       # "1 episode", not "1 episodes"

    def test_badge_for_listened_episode(self):
        ep = EpisodePage(raw_dir="x/y", date="2026-01-01", show=None,
                         listened=True, played_up_to=0, duration_min=0,
                         guests=[], transcript_source=None, body="", hook="")
        threads = [Thread(name="X", summary="Y", episode_slugs=["a", "b"])]
        md = format_threads_md(threads, [], date_str="2026-05-14",
                               episode_lookup={"a": ep, "b": ep})
        assert "🎧" in md
        assert "⚪" not in md

    def test_badge_for_unplayed_episode(self):
        ep = EpisodePage(raw_dir="x/y", date="2026-01-01", show=None,
                         listened=False, played_up_to=0, duration_min=0,
                         guests=[], transcript_source=None, body="", hook="")
        threads = [Thread(name="X", summary="Y", episode_slugs=["a", "b"])]
        md = format_threads_md(threads, [], date_str="2026-05-14",
                               episode_lookup={"a": ep, "b": ep})
        assert "⚪" in md

    def test_hook_snippet_appears(self):
        ep = EpisodePage(raw_dir="x/y", date="2026-01-01", show="My Show",
                         listened=True, played_up_to=0, duration_min=0,
                         guests=[], transcript_source=None, body="",
                         hook="🎧 [[shows/foo]] — Alice argues that bar.")
        threads = [Thread(name="X", summary="Y", episode_slugs=["a", "b"])]
        md = format_threads_md(threads, [], date_str="2026-05-14",
                               episode_lookup={"a": ep, "b": ep})
        assert "Alice argues that bar" in md


# ---------- _extract_json ----------

class TestExtractJson:
    def test_clean_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_json_with_prose_around(self):
        text = "Sure, here's the result:\n\n{\"a\": 1, \"b\": [2, 3]}\n\nLet me know."
        assert _extract_json(text) == {"a": 1, "b": [2, 3]}

    def test_nested_braces(self):
        text = 'preamble {"outer": {"inner": "value"}} suffix'
        assert _extract_json(text) == {"outer": {"inner": "value"}}

    def test_no_json_returns_none(self):
        assert _extract_json("just prose") is None


# ---------- parse_llm_response ----------

class TestParseLlmResponse:
    def test_basic_parse(self):
        text = json.dumps({
            "threads": [
                {"name": "T1", "summary": "S1", "episode_slugs": ["a", "b"]},
            ],
            "uncategorized": ["c"],
        })
        threads, uncat = parse_llm_response(text, valid_slugs={"a", "b", "c"})
        assert len(threads) == 1
        assert threads[0].name == "T1"
        assert threads[0].episode_slugs == ["a", "b"]
        assert uncat == ["c"]

    def test_hallucinated_slugs_dropped(self):
        text = json.dumps({
            "threads": [
                {"name": "T1", "summary": "S1",
                 "episode_slugs": ["a", "b", "hallucinated"]},
            ],
            "uncategorized": [],
        })
        threads, _ = parse_llm_response(text, valid_slugs={"a", "b"})
        assert threads[0].episode_slugs == ["a", "b"]

    def test_too_small_thread_demoted_to_uncategorized(self):
        text = json.dumps({
            "threads": [
                {"name": "T1", "summary": "", "episode_slugs": ["a"]},
            ],
            "uncategorized": [],
        })
        threads, uncat = parse_llm_response(text, valid_slugs={"a", "b"})
        assert threads == []
        assert "a" in uncat
        assert "b" in uncat  # b wasn't placed anywhere → must go to uncat

    def test_missing_episodes_added_to_uncategorized(self):
        """If the LLM forgets an episode, we add it to uncategorized
        instead of losing it silently."""
        text = json.dumps({
            "threads": [
                {"name": "T1", "summary": "", "episode_slugs": ["a", "b"]},
            ],
            "uncategorized": [],
        })
        _, uncat = parse_llm_response(text, valid_slugs={"a", "b", "c", "d"})
        assert set(uncat) == {"c", "d"}

    def test_dedup_uncat(self):
        text = json.dumps({
            "threads": [],
            "uncategorized": ["a", "a", "b"],
        })
        _, uncat = parse_llm_response(text, valid_slugs={"a", "b"})
        assert uncat == ["a", "b"]


# ---------- synthesize_threads (LLM injected) ----------

class TestSynthesizeThreads:
    def _ep(self, **kw) -> EpisodePage:
        defaults = dict(raw_dir="x/y", date="2026-01-01", show=None,
                        listened=True, played_up_to=0, duration_min=0,
                        guests=[], transcript_source=None, body="", hook="")
        defaults.update(kw)
        return EpisodePage(**defaults)

    def test_single_episode_skips_llm(self):
        """No clustering possible with 1 episode."""
        threads, uncat = synthesize_threads(
            [("ep1", self._ep())],
            {}, {},
            chat_fn=lambda p: pytest.fail("LLM should not be called"),
        )
        assert threads == []
        assert uncat == ["ep1"]

    def test_llm_response_parsed(self):
        eps = [("a", self._ep()), ("b", self._ep()), ("c", self._ep())]
        llm_output = json.dumps({
            "threads": [{"name": "T1", "summary": "S",
                         "episode_slugs": ["a", "b"]}],
            "uncategorized": ["c"],
        })

        def fake_chat(prompt: str) -> tuple[str, llm.Usage]:
            return llm_output, llm.Usage()

        threads, uncat = synthesize_threads(
            eps, {}, {},
            chat_fn=fake_chat,
        )
        assert len(threads) == 1
        assert threads[0].episode_slugs == ["a", "b"]
        assert uncat == ["c"]

    def test_llm_failure_falls_back_to_mechanical(self):
        """LLMError twice → fallback uses bridges to make threads."""
        eps = [("a", self._ep()), ("b", self._ep()), ("c", self._ep())]
        person_bridges = {"alice": ["a", "b"]}

        def failing_chat(prompt: str) -> tuple[str, llm.Usage]:
            raise llm.LLMError("boom")

        threads, uncat = synthesize_threads(
            eps, person_bridges, {},
            chat_fn=failing_chat,
        )
        # Mechanical fallback should produce one thread for alice
        assert len(threads) == 1
        assert "alice" in threads[0].name.lower()
        assert set(threads[0].episode_slugs) == {"a", "b"}
        assert uncat == ["c"]

    def test_chat_fn_failure_falls_back(self):
        """chat_fn raising on every call triggers fallback."""
        eps = [("a", self._ep()), ("b", self._ep())]

        def bad_chat(prompt: str) -> tuple[str, llm.Usage]:
            raise llm.LLMError("no key")

        threads, uncat = synthesize_threads(
            eps, {"alice": ["a", "b"]}, {},
            chat_fn=bad_chat,
        )
        assert len(threads) == 1

    def test_empty_threads_with_bridges_retries_then_entity_fallback(self):
        """0 threads despite shared entities → retry once, then entity grouping."""
        eps = [("a", self._ep()), ("b", self._ep())]
        empty = json.dumps({"threads": [], "uncategorized": ["a", "b"]})
        calls = {"n": 0}

        def empty_chat(prompt: str) -> tuple[str, llm.Usage]:
            calls["n"] += 1
            return empty, llm.Usage()

        threads, uncat = synthesize_threads(
            eps, {"elon-musk": ["a", "b"]}, {}, chat_fn=empty_chat,
        )
        assert calls["n"] == 2                      # retried once
        assert len(threads) == 1                    # entity fallback engaged
        assert threads[0].name == "Elon Musk"
        assert set(threads[0].episode_slugs) == {"a", "b"}

    def test_empty_threads_without_bridges_returns_empty_no_retry(self):
        """No shared entities → empty is legitimate; don't retry or fabricate."""
        eps = [("a", self._ep()), ("b", self._ep())]
        empty = json.dumps({"threads": [], "uncategorized": ["a", "b"]})
        calls = {"n": 0}

        def empty_chat(prompt: str) -> tuple[str, llm.Usage]:
            calls["n"] += 1
            return empty, llm.Usage()

        threads, uncat = synthesize_threads(eps, {}, {}, chat_fn=empty_chat)
        assert calls["n"] == 1                      # no retry
        assert threads == []
        assert set(uncat) == {"a", "b"}


# ---------- regression: badge + fallback-copy hygiene (2026-06-20) ----------

def _badge_ep(*, listened=False, played_up_to=0, duration_min=0):
    return EpisodePage(raw_dir="x/y", date="2026-01-01", show=None,
                       listened=listened, played_up_to=played_up_to,
                       duration_min=duration_min, guests=[],
                       transcript_source=None, body="", hook="")


def test_badge_for_negligible_progress_shows_zero_percent():
    # 4 seconds of a 17-min episode → "🎧 0%" (not fully listened → show the %),
    # using headphones (not ▶) so it doesn't read as the YouTube ▶️ badge.
    from podmind.threads import _badge_for
    assert _badge_for(_badge_ep(played_up_to=4, duration_min=17), 17 * 60) == "🎧 0%"


def test_badge_for_real_progress_shows_percent():
    from podmind.threads import _badge_for
    assert _badge_for(_badge_ep(played_up_to=153, duration_min=17), 17 * 60) == "🎧 15%"


def test_badge_for_unplayed_is_circle():
    from podmind.threads import _badge_for
    assert _badge_for(_badge_ep(played_up_to=0, duration_min=17), 17 * 60) == "⚪"


def test_entity_name_dekebabs():
    from podmind.threads_llm import _entity_name
    assert _entity_name("elon-musk") == "Elon Musk"


def test_strip_hook_prefix_shared_helper():
    from podmind.threads import strip_hook_prefix
    assert strip_hook_prefix("🎧 [[shows/x]] — The real sentence.") == "The real sentence."
    assert strip_hook_prefix("No prefix here.") == "No prefix here."
    assert strip_hook_prefix("") == ""


_EP_BODY = """🎧 [[shows/x]] — A hook sentence.

## Key takeaways

- First takeaway about SpaceX.
- Second takeaway about Iran.
- Third takeaway about AI.
- Fourth takeaway should be dropped.

## Notable quotes

- "not a takeaway"
"""


def test_key_takeaways_extracts_capped_bullets():
    ep = _badge_ep()
    ep = EpisodePage(raw_dir="x/y", date="2026-01-01", show="S", listened=True,
                     played_up_to=0, duration_min=30, guests=[],
                     transcript_source=None, body=_EP_BODY, hook="A hook sentence.")
    got = key_takeaways(ep)
    assert got == ["First takeaway about SpaceX.",
                   "Second takeaway about Iran.",
                   "Third takeaway about AI."]   # capped at 3, quotes excluded


def test_key_takeaways_none_when_section_absent():
    ep = EpisodePage(raw_dir="x/y", date="2026-01-01", show="S", listened=True,
                     played_up_to=0, duration_min=30, guests=[],
                     transcript_source=None, body="🎧 just a hook, no sections.",
                     hook="just a hook")
    assert key_takeaways(ep) == []


def test_prompt_includes_takeaways():
    ep = EpisodePage(raw_dir="x/y", date="2026-01-01", show="S", listened=True,
                     played_up_to=0, duration_min=30, guests=[],
                     transcript_source=None, body=_EP_BODY, hook="A hook sentence.")
    block = format_episodes_for_prompt([("ep-a", ep)])
    assert "takeaways:" in block
    assert "First takeaway about SpaceX." in block
    assert "Fourth takeaway should be dropped." not in block


def test_fallback_copy_has_no_internal_leakage():
    # LLM failure must not leak internals into the reader-facing digest.
    eps = [("a", _badge_ep(listened=True)), ("b", _badge_ep(listened=True)),
           ("c", _badge_ep(listened=True))]

    def failing_chat(prompt: str) -> tuple[str, llm.Usage]:
        raise llm.LLMError("no JSON object in LLM response")

    threads, uncat = synthesize_threads(
        eps, {"elon-musk": ["a", "b"]}, {}, chat_fn=failing_chat,
    )
    assert len(threads) == 1
    t = threads[0]
    assert t.name == "Elon Musk"
    assert "Person:" not in t.name
    assert t.summary == "Episodes connected by Elon Musk."
    for leak in ("LLM fallback", "ValueError", "no JSON object"):
        assert leak not in t.summary
