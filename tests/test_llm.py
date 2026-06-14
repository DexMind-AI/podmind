"""Tests for podmind.llm — config resolution and providers."""
import asyncio
import json

import httpx
import pytest

from podmind import llm, secrets


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No PODMIND_LLM_* env vars; secrets file in tmp."""
    for var in ("PODMIND_LLM_PROVIDER", "PODMIND_LLM_BASE_URL",
                "PODMIND_LLM_MODEL", "PODMIND_LLM_API_KEY",
                "PODMIND_EMBED_BASE_URL", "PODMIND_EMBED_MODEL",
                "PODMIND_EMBED_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    sf = tmp_path / "secrets.json"
    monkeypatch.setenv("PODMIND_SECRETS", str(sf))
    return sf


class TestSecretsResolution:
    def test_env_var_overrides_default_location(self, clean_env):
        clean_env.write_text(json.dumps({"foo": "bar"}))
        assert secrets.load()["foo"] == "bar"

    def test_missing_file_loads_empty(self, clean_env):
        assert secrets.load() == {}


class TestChatConfig:
    def test_defaults_to_deepseek_with_legacy_key(self, clean_env):
        clean_env.write_text(json.dumps({"deepseek_api_key": "sk-legacy"}))
        cfg = llm.resolve_chat_config()
        assert cfg.provider == "openai-compat"
        assert cfg.base_url == "https://api.deepseek.com/v1"
        assert cfg.model == "deepseek-chat"
        assert cfg.api_key == "sk-legacy"

    def test_secrets_llm_keys_beat_legacy(self, clean_env):
        clean_env.write_text(json.dumps({
            "deepseek_api_key": "sk-legacy",
            "llm_api_key": "sk-new", "llm_model": "gpt-5.2-mini",
            "llm_base_url": "https://api.openai.com/v1",
        }))
        cfg = llm.resolve_chat_config()
        assert (cfg.model, cfg.api_key) == ("gpt-5.2-mini", "sk-new")

    def test_env_beats_secrets(self, clean_env, monkeypatch):
        clean_env.write_text(json.dumps({"llm_api_key": "sk-secrets"}))
        monkeypatch.setenv("PODMIND_LLM_API_KEY", "sk-env")
        monkeypatch.setenv("PODMIND_LLM_MODEL", "llama3.3")
        monkeypatch.setenv("PODMIND_LLM_BASE_URL", "http://localhost:11434/v1")
        cfg = llm.resolve_chat_config()
        assert (cfg.api_key, cfg.model) == ("sk-env", "llama3.3")
        assert cfg.base_url == "http://localhost:11434/v1"

    def test_no_key_raises_with_actionable_message(self, clean_env):
        with pytest.raises(RuntimeError, match="PODMIND_LLM_API_KEY"):
            llm.resolve_chat_config()

    def test_anthropic_provider_selectable(self, clean_env, monkeypatch):
        monkeypatch.setenv("PODMIND_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("PODMIND_LLM_API_KEY", "sk-ant")
        monkeypatch.setenv("PODMIND_LLM_MODEL", "claude-sonnet-4-6")
        cfg = llm.resolve_chat_config()
        assert cfg.provider == "anthropic"
        assert cfg.base_url == "https://api.anthropic.com"

    def test_empty_env_var_treated_as_unset(self, clean_env, monkeypatch):
        clean_env.write_text(json.dumps({"llm_api_key": "sk-secrets",
                                         "llm_model": "from-secrets"}))
        monkeypatch.setenv("PODMIND_LLM_MODEL", "")
        monkeypatch.setenv("PODMIND_LLM_API_KEY", "")
        cfg = llm.resolve_chat_config()
        assert (cfg.model, cfg.api_key) == ("from-secrets", "sk-secrets")


class TestEmbedConfig:
    def test_defaults_to_openrouter_with_legacy_env_key(self, clean_env, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
        cfg = llm.resolve_embed_config()
        assert cfg.base_url == "https://openrouter.ai/api/v1"
        assert cfg.model == "openai/text-embedding-3-small"
        assert cfg.api_key == "sk-or"

    def test_podmind_embed_env_beats_openrouter_key(self, clean_env, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
        monkeypatch.setenv("PODMIND_EMBED_API_KEY", "sk-direct")
        monkeypatch.setenv("PODMIND_EMBED_BASE_URL", "https://api.openai.com/v1")
        monkeypatch.setenv("PODMIND_EMBED_MODEL", "text-embedding-3-small")
        cfg = llm.resolve_embed_config()
        assert cfg.api_key == "sk-direct"
        assert cfg.base_url == "https://api.openai.com/v1"


def _openai_response(content="{}", usage=None):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestOpenAICompatProvider:
    CFG = llm.LLMConfig("openai-compat", "https://api.deepseek.com/v1",
                        "deepseek-chat", "sk-test")

    def test_chat_posts_to_chat_completions_with_bearer(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_openai_response('{"ok": true}'))

        provider = llm.OpenAICompatProvider(self.CFG)
        content, usage = provider.chat("hi", client=_mock_client(handler))
        assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
        assert seen["auth"] == "Bearer sk-test"
        assert seen["body"]["model"] == "deepseek-chat"
        assert seen["body"]["response_format"] == {"type": "json_object"}
        assert content == '{"ok": true}'
        assert usage.input_tokens == 100 and usage.output_tokens == 20

    def test_json_mode_off_omits_response_format(self):
        def handler(request):
            assert "response_format" not in json.loads(request.content)
            return httpx.Response(200, json=_openai_response("plain text"))

        provider = llm.OpenAICompatProvider(self.CFG)
        content, _ = provider.chat("hi", json_mode=False, client=_mock_client(handler))
        assert content == "plain text"

    def test_deepseek_cache_fields_normalize(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 5,
                 "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
        provider = llm.OpenAICompatProvider(self.CFG)
        _, u = provider.chat("hi", client=_mock_client(
            lambda r: httpx.Response(200, json=_openai_response(usage=usage))))
        assert u.cached_input_tokens == 80 and u.input_tokens == 100

    def test_openai_cached_tokens_normalize(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 5,
                 "prompt_tokens_details": {"cached_tokens": 64}}
        provider = llm.OpenAICompatProvider(self.CFG)
        _, u = provider.chat("hi", client=_mock_client(
            lambda r: httpx.Response(200, json=_openai_response(usage=usage))))
        assert u.cached_input_tokens == 64

    def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=_openai_response("ok"))

        provider = llm.OpenAICompatProvider(self.CFG)
        content, _ = provider.chat("hi", client=_mock_client(handler))
        assert content == "ok" and calls["n"] == 2

    def test_exhausted_retries_raise(self, monkeypatch):
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)
        provider = llm.OpenAICompatProvider(self.CFG)
        with pytest.raises(llm.LLMError):
            provider.chat("hi", retries=1,
                          client=_mock_client(lambda r: httpx.Response(500)))

    def test_achat_round_trip(self):
        import asyncio

        async def go():
            transport = httpx.MockTransport(
                lambda r: httpx.Response(200, json=_openai_response("async-ok")))
            provider = llm.OpenAICompatProvider(self.CFG)
            async with httpx.AsyncClient(transport=transport) as client:
                return await provider.achat(client, "hi")

        content, _ = asyncio.run(go())
        assert content == "async-ok"


class TestAnthropicProvider:
    CFG = llm.LLMConfig("anthropic", "https://api.anthropic.com",
                        "claude-sonnet-4-6", "sk-ant-test")

    @staticmethod
    def _response(text='{"ok": true}'):
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 90, "output_tokens": 10,
                      "cache_read_input_tokens": 30},
        }

    def test_posts_messages_with_anthropic_headers(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["key"] = request.headers["x-api-key"]
            seen["version"] = request.headers["anthropic-version"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=self._response())

        provider = llm.AnthropicProvider(self.CFG)
        content, usage = provider.chat("hi", client=_mock_client(handler))
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        assert seen["key"] == "sk-ant-test"
        assert seen["body"]["model"] == "claude-sonnet-4-6"
        assert seen["body"]["max_tokens"] == 4000
        assert content == '{"ok": true}'
        # Anthropic reports cache reads SEPARATELY from input_tokens;
        # normalized input_tokens includes them.
        assert usage.input_tokens == 120
        assert usage.cached_input_tokens == 30

    def test_json_mode_appends_json_instruction(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["messages"][0]["content"].endswith(
                "Respond with a single JSON object only — no prose, no fences.")
            return httpx.Response(200, json=self._response())

        llm.AnthropicProvider(self.CFG).chat("hi", client=_mock_client(handler))


class TestFactory:
    def test_get_provider_dispatches_on_provider_field(self):
        oa = llm.get_provider(llm.LLMConfig("openai-compat", "u", "m", "k"))
        an = llm.get_provider(llm.LLMConfig("anthropic", "u", "m", "k"))
        assert isinstance(oa, llm.OpenAICompatProvider)
        assert isinstance(an, llm.AnthropicProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="bogus"):
            llm.get_provider(llm.LLMConfig("bogus", "u", "m", "k"))


class TestChatJson:
    def test_chat_json_extracts_object(self):
        provider = llm.OpenAICompatProvider(TestOpenAICompatProvider.CFG)
        client = _mock_client(lambda r: httpx.Response(
            200, json=_openai_response('```json\n{"a": 1}\n```')))
        obj, usage = llm.chat_json(provider, "hi", client=client)
        assert obj == {"a": 1}


class TestCost:
    def test_deepseek_cost_from_usage(self):
        u = llm.Usage(input_tokens=1_000_000, cached_input_tokens=0,
                      output_tokens=1_000_000)
        assert llm.cost_usd("deepseek-chat", u) == pytest.approx(0.27 + 1.10)

    def test_cached_tokens_priced_at_hit_rate(self):
        u = llm.Usage(input_tokens=1_000_000, cached_input_tokens=1_000_000,
                      output_tokens=0)
        assert llm.cost_usd("deepseek-chat", u) == pytest.approx(0.07)

    def test_unknown_model_returns_none(self):
        assert llm.cost_usd("gpt-5.2-mini", llm.Usage(10, 0, 10)) is None


class TestRetrySemantics:
    CFG = llm.LLMConfig("openai-compat", "https://api.deepseek.com/v1",
                        "deepseek-chat", "sk-test")

    def test_4xx_raises_immediately_without_retry(self, monkeypatch):
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401, text="unauthorized")

        provider = llm.OpenAICompatProvider(self.CFG)
        with pytest.raises(llm.LLMError, match="401"):
            provider.chat("hi", retries=2, client=_mock_client(handler))
        assert calls["n"] == 1

    def test_malformed_200_raises_immediately_with_snippet(self, monkeypatch):
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, text="<html>gateway oops</html>")

        provider = llm.OpenAICompatProvider(self.CFG)
        with pytest.raises(llm.LLMError, match="gateway oops"):
            provider.chat("hi", retries=2, client=_mock_client(handler))
        assert calls["n"] == 1

    def test_429_is_retried(self, monkeypatch):
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, json=_openai_response("ok"))

        provider = llm.OpenAICompatProvider(self.CFG)
        content, _ = provider.chat("hi", retries=2, client=_mock_client(handler))
        assert content == "ok"
        assert calls["n"] == 2

    def test_async_retries_then_succeeds(self, monkeypatch):
        async def async_noop(s):
            pass

        monkeypatch.setattr(llm.asyncio, "sleep", async_noop)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=_openai_response("async-retry-ok"))

        async def go():
            transport = httpx.MockTransport(handler)
            provider = llm.OpenAICompatProvider(self.CFG)
            async with httpx.AsyncClient(transport=transport) as client:
                return await provider.achat(client, "hi", retries=2)

        content, _ = asyncio.run(go())
        assert content == "async-retry-ok"
        assert calls["n"] == 2
