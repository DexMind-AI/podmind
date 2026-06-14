"""Provider-agnostic LLM access: chat + embedding configuration and clients.

Two wire protocols cover the practical provider space:

* ``openai-compat`` — DeepSeek, OpenAI, OpenRouter, Ollama, LM Studio, Groq:
  anything speaking ``POST {base_url}/chat/completions``.
* ``anthropic`` — Claude models via ``POST {base_url}/v1/messages``.

Configuration resolution, per field (first hit wins):

1. env — ``PODMIND_LLM_PROVIDER`` / ``_BASE_URL`` / ``_MODEL`` / ``_API_KEY``
   (embeddings: ``PODMIND_EMBED_BASE_URL`` / ``_MODEL`` / ``_API_KEY``)
2. secrets.json — ``llm_provider`` / ``llm_base_url`` / ``llm_model`` /
   ``llm_api_key`` (embeddings: ``embed_*``); legacy ``deepseek_api_key``
   and ``$OPENROUTER_API_KEY`` are honored.
3. defaults — DeepSeek V4 chat, OpenRouter embeddings (the tested,
   cost-measured paths; see README "Cost discipline").

Empty values count as unset: an exported-but-empty env var falls through to
the next source.
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from podmind import secrets

DEFAULT_CHAT = ("openai-compat", "https://api.deepseek.com/v1", "deepseek-chat")
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"
DEFAULT_EMBED = ("openai-compat", "https://openrouter.ai/api/v1",
                 "openai/text-embedding-3-small")


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0  # subset of input_tokens served from cache
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class LLMConfig:
    provider: str  # "openai-compat" | "anthropic"
    base_url: str
    model: str
    api_key: str


def _pick(env_var: str, secret_key: str, default: str | None) -> str | None:
    return os.environ.get(env_var) or secrets.get(secret_key) or default


def resolve_chat_config() -> LLMConfig:
    provider = _pick("PODMIND_LLM_PROVIDER", "llm_provider", DEFAULT_CHAT[0])
    default_url = DEFAULT_ANTHROPIC_URL if provider == "anthropic" else DEFAULT_CHAT[1]
    base_url = _pick("PODMIND_LLM_BASE_URL", "llm_base_url", default_url)
    model = _pick("PODMIND_LLM_MODEL", "llm_model", DEFAULT_CHAT[2])
    api_key = (os.environ.get("PODMIND_LLM_API_KEY")
               or secrets.get("llm_api_key")
               or secrets.get("deepseek_api_key"))
    if not api_key:
        raise RuntimeError(
            "No LLM API key. Set PODMIND_LLM_API_KEY, or put llm_api_key "
            f"(or legacy deepseek_api_key) in {secrets.secrets_path()}"
        )
    return LLMConfig(provider, base_url.rstrip("/"), model, api_key)


def resolve_embed_config() -> LLMConfig:
    base_url = _pick("PODMIND_EMBED_BASE_URL", "embed_base_url", DEFAULT_EMBED[1])
    model = _pick("PODMIND_EMBED_MODEL", "embed_model", DEFAULT_EMBED[2])
    api_key = (os.environ.get("PODMIND_EMBED_API_KEY")
               or secrets.get("embed_api_key")
               or os.environ.get("OPENROUTER_API_KEY"))
    if not api_key:
        raise RuntimeError(
            "No embeddings API key. Set PODMIND_EMBED_API_KEY (or legacy "
            "OPENROUTER_API_KEY), or embed_api_key in "
            f"{secrets.secrets_path()}"
        )
    # Embeddings are always openai-compat — Anthropic has no embeddings API.
    return LLMConfig("openai-compat", base_url.rstrip("/"), model, api_key)


class LLMError(RuntimeError):
    """Persistent LLM API failure after retries."""


# (cached_input, fresh_input, output) USD per 1M tokens, by model prefix.
# Cost reporting is best-effort: unknown models report tokens, not dollars.
PRICING_PER_M: dict[str, tuple[float, float, float]] = {
    "deepseek-chat": (0.07, 0.27, 1.10),
}


def cost_usd(model: str, usage: Usage) -> float | None:
    for prefix, (hit, miss, out) in PRICING_PER_M.items():
        if model.startswith(prefix):
            fresh = usage.input_tokens - usage.cached_input_tokens
            return (usage.cached_input_tokens / 1e6) * hit \
                 + (fresh / 1e6) * miss \
                 + (usage.output_tokens / 1e6) * out
    return None


def _parse_openai(j: dict) -> tuple[str, Usage]:
    content = j["choices"][0]["message"]["content"]
    u = j.get("usage") or {}
    if "prompt_cache_hit_tokens" in u:
        cached = u["prompt_cache_hit_tokens"]
    else:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return content, Usage(u.get("prompt_tokens", 0), cached,
                          u.get("completion_tokens", 0))


def _is_transient(e: httpx.HTTPError) -> bool:
    """True for errors worth retrying: transport failures + 5xx + 429."""
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code >= 500 or code == 429
    return False


def _post_with_retries(
    client: httpx.Client | None,
    url: str,
    headers: dict[str, str],
    body: dict,
    timeout: float,
    retries: int,
    # parse may raise only KeyError/JSONDecodeError on malformed bodies;
    # those are converted to LLMError without retry.
    parse: Callable[[dict], tuple[str, Usage]],
    model: str = "",
) -> tuple[str, Usage]:
    own = client is None
    client = client or httpx.Client()
    ctx = f"{url} [{model}]" if model else url
    try:
        for attempt in range(retries + 1):
            try:
                r = client.post(url, headers=headers, json=body, timeout=timeout)
                r.raise_for_status()
                try:
                    return parse(r.json())
                except (KeyError, _json.JSONDecodeError) as e:
                    snippet = r.text[:200]
                    raise LLMError(
                        f"{ctx}: {type(e).__name__}: {e} — body: {snippet!r}"
                    ) from e
            except LLMError:
                raise
            except httpx.HTTPStatusError as e:
                if not _is_transient(e):
                    raise LLMError(
                        f"{ctx}: {type(e).__name__}: {e}"
                    ) from e
                if attempt == retries:
                    raise LLMError(f"{ctx}: {type(e).__name__}: {e}") from e
                time.sleep(2 ** attempt)
            except httpx.TransportError as e:
                if attempt == retries:
                    raise LLMError(f"{ctx}: {type(e).__name__}: {e}") from e
                time.sleep(2 ** attempt)
    finally:
        if own:
            client.close()
    raise LLMError("unreachable")


async def _apost_with_retries(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict,
    timeout: float,
    retries: int,
    # parse may raise only KeyError/JSONDecodeError on malformed bodies;
    # those are converted to LLMError without retry.
    parse: Callable[[dict], tuple[str, Usage]],
    model: str = "",
) -> tuple[str, Usage]:
    ctx = f"{url} [{model}]" if model else url
    for attempt in range(retries + 1):
        try:
            r = await client.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            try:
                return parse(r.json())
            except (KeyError, _json.JSONDecodeError) as e:
                snippet = r.text[:200]
                raise LLMError(
                    f"{ctx}: {type(e).__name__}: {e} — body: {snippet!r}"
                ) from e
        except LLMError:
            raise
        except httpx.HTTPStatusError as e:
            if not _is_transient(e):
                raise LLMError(
                    f"{ctx}: {type(e).__name__}: {e}"
                ) from e
            if attempt == retries:
                raise LLMError(f"{ctx}: {type(e).__name__}: {e}") from e
            await asyncio.sleep(2 ** attempt)
        except httpx.TransportError as e:
            if attempt == retries:
                raise LLMError(f"{ctx}: {type(e).__name__}: {e}") from e
            await asyncio.sleep(2 ** attempt)
    raise LLMError("unreachable")


class OpenAICompatProvider:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    def _url(self) -> str:
        return f"{self.cfg.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json"}

    def _body(self, prompt: str, *, json_mode: bool, temperature: float,
              max_tokens: int) -> dict:
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def chat(self, prompt: str, *, json_mode: bool = True,
             temperature: float = 0.3, max_tokens: int = 4000,
             timeout: float = 180, retries: int = 2,
             client: httpx.Client | None = None) -> tuple[str, Usage]:
        body = self._body(prompt, json_mode=json_mode,
                          temperature=temperature, max_tokens=max_tokens)
        return _post_with_retries(client, self._url(), self._headers(),
                                  body, timeout, retries, _parse_openai,
                                  self.cfg.model)

    async def achat(self, client: httpx.AsyncClient, prompt: str, *,
                    json_mode: bool = True, temperature: float = 0.3,
                    max_tokens: int = 4000, timeout: float = 180,
                    retries: int = 2) -> tuple[str, Usage]:
        body = self._body(prompt, json_mode=json_mode,
                          temperature=temperature, max_tokens=max_tokens)
        return await _apost_with_retries(client, self._url(), self._headers(),
                                         body, timeout, retries, _parse_openai,
                                         self.cfg.model)


_JSON_INSTRUCTION = ("\n\nRespond with a single JSON object only — "
                     "no prose, no fences.")


def _parse_anthropic(j: dict) -> tuple[str, Usage]:
    content = "".join(b["text"] for b in j["content"]
                      if b.get("type") == "text")
    u = j.get("usage") or {}
    cached = u.get("cache_read_input_tokens", 0)
    return content, Usage(u.get("input_tokens", 0) + cached, cached,
                          u.get("output_tokens", 0))


class AnthropicProvider:
    """Anthropic /v1/messages. No native JSON mode — json_mode appends an
    instruction; pair with llm_json.extract_json (chat_json does both)."""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    def _url(self) -> str:
        return f"{self.cfg.base_url}/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.cfg.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"}

    def _body(self, prompt: str, *, json_mode: bool, temperature: float,
              max_tokens: int) -> dict:
        if json_mode:
            prompt = prompt + _JSON_INSTRUCTION
        return {"model": self.cfg.model, "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}]}

    def chat(self, prompt: str, *, json_mode: bool = True,
             temperature: float = 0.3, max_tokens: int = 4000,
             timeout: float = 180, retries: int = 2,
             client: httpx.Client | None = None) -> tuple[str, Usage]:
        body = self._body(prompt, json_mode=json_mode,
                          temperature=temperature, max_tokens=max_tokens)
        return _post_with_retries(client, self._url(), self._headers(),
                                  body, timeout, retries, _parse_anthropic,
                                  self.cfg.model)

    async def achat(self, client: httpx.AsyncClient, prompt: str, *,
                    json_mode: bool = True, temperature: float = 0.3,
                    max_tokens: int = 4000, timeout: float = 180,
                    retries: int = 2) -> tuple[str, Usage]:
        body = self._body(prompt, json_mode=json_mode,
                          temperature=temperature, max_tokens=max_tokens)
        return await _apost_with_retries(client, self._url(), self._headers(),
                                         body, timeout, retries, _parse_anthropic,
                                         self.cfg.model)


class LLMProvider(Protocol):
    cfg: LLMConfig

    def chat(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...,
             max_tokens: int = ..., timeout: float = ..., retries: int = ...,
             client: httpx.Client | None = ...) -> tuple[str, Usage]: ...

    async def achat(self, client: httpx.AsyncClient, prompt: str, *,
                    json_mode: bool = ..., temperature: float = ...,
                    max_tokens: int = ..., timeout: float = ...,
                    retries: int = ...) -> tuple[str, Usage]: ...


def get_provider(cfg: LLMConfig | None = None) -> LLMProvider:
    cfg = cfg or resolve_chat_config()
    if cfg.provider == "openai-compat":
        return OpenAICompatProvider(cfg)
    if cfg.provider == "anthropic":
        return AnthropicProvider(cfg)
    raise ValueError(f"Unknown LLM provider {cfg.provider!r} "
                     "(expected 'openai-compat' or 'anthropic')")


def chat_json(provider: LLMProvider, prompt: str, **kwargs) -> tuple[dict | list | None, Usage]:
    """chat() + tolerant JSON extraction (handles fences/prose envelopes)."""
    from podmind.llm_json import extract_json
    content, usage = provider.chat(prompt, **kwargs)
    return extract_json(content), usage
