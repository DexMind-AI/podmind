"""Extract structured JSON from LLM responses.

LLMs return JSON-shaped text with three common envelopes:
  1. Clean JSON only (modern OpenAI-compat `response_format=json_object` mode)
  2. JSON wrapped in a code fence (```json ... ```)
  3. JSON inside prose ("Here is the result: { ... }. Hope this helps!")

This module handles all three. The OpenAI-compat code paths in `summarize.py`
already get clean JSON via `response_format`; the Anthropic CLI subprocess
path in `merge_topic_opus.py` and the calls in `merge_topic_ai.py`
need the more permissive extractor.
"""
from __future__ import annotations

import json
import re

# Match a balanced `{...}` block — non-greedy + balanced via simple state machine
# below. Pure regex can't handle nested braces; we scan manually after a coarse
# regex finds the start.
_OBJECT_START = re.compile(r"\{", re.S)
_CODE_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.S)


def extract_json(text: str) -> dict | list | None:
    """Pull the first JSON object/array out of an LLM response.

    Returns None if no parseable JSON is found. Tries strategies in order:
      1. Strip whitespace, parse the whole string
      2. Strip a markdown code fence, parse the contents
      3. Find the first {...} block via regex, parse it

    All strategies use `json.loads`; no eval, no yaml fallback.
    """
    if not text:
        return None
    # 1. Clean JSON
    stripped = text.strip()
    if stripped:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # 2. Code fence
    fence = _CODE_FENCE.search(text)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 3. Walk for first balanced {...} block
    candidate = _first_balanced_object(text)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return None


def _first_balanced_object(text: str) -> str | None:
    """Return the first balanced `{...}` substring, respecting string literals.

    Pure regex can't handle nested braces. This walks the text once tracking
    depth and whether we're inside a string. Comments and other JSON5 features
    are not supported."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
