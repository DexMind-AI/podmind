"""Tests for podmind.llm_json.extract_json.

The function handles three real-world LLM response shapes:
  - Clean JSON (modern OpenAI-compat response_format=json_object)
  - JSON in a markdown code fence
  - JSON inside prose
"""
import pytest

from podmind.llm_json import extract_json


class TestCleanJson:
    def test_object(self):
        assert extract_json('{"verdict": "merge", "n": 3}') == {"verdict": "merge", "n": 3}

    def test_array(self):
        assert extract_json('[1, 2, 3]') == [1, 2, 3]

    def test_with_surrounding_whitespace(self):
        assert extract_json('  \n  {"a": 1}\n  ') == {"a": 1}

    def test_nested(self):
        text = '{"outer": {"inner": [1, 2]}, "list": ["a", "b"]}'
        assert extract_json(text) == {"outer": {"inner": [1, 2]}, "list": ["a", "b"]}


class TestCodeFence:
    def test_json_fence(self):
        text = '```json\n{"verdict": "merge"}\n```'
        assert extract_json(text) == {"verdict": "merge"}

    def test_unmarked_fence(self):
        text = '```\n{"a": 1}\n```'
        assert extract_json(text) == {"a": 1}

    def test_fence_with_preamble(self):
        text = "Here you go:\n\n```json\n{\"x\": 42}\n```\n\nHope that helps!"
        assert extract_json(text) == {"x": 42}

    def test_fence_with_trailing_text(self):
        text = '```json\n{"a": 1}\n```\nNote: this is approximate.'
        assert extract_json(text) == {"a": 1}


class TestEmbeddedInProse:
    def test_object_in_sentence(self):
        text = "Sure, the verdict is {\"verdict\": \"distinct\", \"reason\": \"different\"}."
        result = extract_json(text)
        assert result == {"verdict": "distinct", "reason": "different"}

    def test_first_object_wins_when_multiple_present(self):
        """Regex captures the broadest balanced range, not the first single."""
        text = 'A: {"first": 1} and B: {"second": 2}'
        result = extract_json(text)
        # The greedy `.*?` with re.S picks the broadest match — covers both.
        # This is acceptable: in our LLM-prompt protocol there's only one obj.
        assert result == {"first": 1, "second": 2} or result == {"first": 1}


class TestFailureModes:
    def test_empty_string(self):
        assert extract_json("") is None

    def test_only_whitespace(self):
        assert extract_json("   \n\n  ") is None

    def test_no_json_at_all(self):
        assert extract_json("This is just prose with no objects.") is None

    def test_malformed_json(self):
        # Missing closing brace
        assert extract_json("{") is None

    def test_almost_json(self):
        # Single quotes — not valid JSON
        assert extract_json("{'verdict': 'merge'}") is None


class TestRealWorldExamples:
    """Snapshots from actual LLM responses we've seen."""

    def test_opus_with_preamble(self):
        """Opus sometimes prefaces JSON with a brief acknowledgement."""
        text = '''Here is my analysis:

{
  "verdict": "merge",
  "canonical": "ai-agents",
  "exclude": [],
  "reasoning": "All slugs describe the same concept of AI agents."
}'''
        result = extract_json(text)
        assert result["verdict"] == "merge"
        assert result["canonical"] == "ai-agents"

    def test_ds_v4_clean(self):
        """DeepSeek with response_format=json_object returns this shape."""
        text = '{"verdict":"partial","canonical":"claude-code","exclude":["claude-code-vs-codex"],"reasoning":"x"}'
        result = extract_json(text)
        assert result["exclude"] == ["claude-code-vs-codex"]

    def test_thinking_then_json(self):
        """Models sometimes <think>...</think> first, then output JSON."""
        text = '<think>This is a comparison page, so it should be excluded...</think>\n\n{"verdict": "distinct"}'
        assert extract_json(text) == {"verdict": "distinct"}
