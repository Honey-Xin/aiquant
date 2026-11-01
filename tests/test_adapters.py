"""适配层测试：MCP 只读客户端、新闻源、Redis 封装、LLM 客户端、日志脱敏。

这些模块都是"外部世界的边界"，全部通过注入的假传输层验证，不依赖网络/子进程。
"""

from __future__ import annotations

import io
import json
import logging
import sys
import types

import httpx
import pytest

from aiquant.label_service.llm.client import HttpLLMClient, LLMError, StaticLLMClient, dumps
from aiquant.label_service.sources import news as news_mod
from aiquant.label_service.sources import okx_mcp as mcp_mod
from aiquant.label_service.sources.news import NewsClient
from aiquant.label_service.sources.okx_mcp import (
    MarketSnapshot,
    McpTool,
    McpToolError,
    OkxMcpReadOnlyClient,
)
from aiquant.label_service.store import build_store
from aiquant.label_service.store.base import StoreError
from aiquant.label_service.store.memory_store import MemoryStore
from aiquant.label_service.store.redis_store import RedisStore
from aiquant.logging_setup import RedactingFilter, get_logger, setup_logging

# =============================================================== OKX MCP


class FakeProc:
    """最小 subprocess 替身：stdin 收集写入，stdout 按行回放。"""

    def __init__(self, lines=None, write_error=None):
        self.stdin = io.StringIO() if write_error is None else _BrokenStdin(write_error)
        self.stdout = io.StringIO("\n".join(lines or []) + "\n" if lines else "")
        self.stderr = io.StringIO("")
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def communicate(self, timeout=None):
        return "", "stderr 摘要"

    def kill(self):  # pragma: no cover - 仅异常收尾
        pass


class _BrokenStdin:
    def __init__(self, error):
        self._error = error

    def write(self, _data):
        raise self._error

    def flush(self):  # pragma: no cover - write 先失败
        pass


def _patch_popen(monkeypatch, fake_or_exc):
    def fake_popen(*_args, **_kwargs):
        if isinstance(fake_or_exc, Exception):
            raise fake_or_exc
        return fake_or_exc

    monkeypatch.setattr(mcp_mod.subprocess, "Popen", fake_popen)


def test_mcp_batch_reads_jsonl_responses(monkeypatch):
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "get_ticker"}]}})
    _patch_popen(monkeypatch, FakeProc(lines=[line, "not-json", ""]))
    client = OkxMcpReadOnlyClient(cmd="okx-trade-mcp --read-only")
    tools = client.list_tools()
    assert [t.name for t in tools] == ["get_ticker"]


def test_mcp_batch_popen_missing_command(monkeypatch):
    _patch_popen(monkeypatch, FileNotFoundError("nope"))
    with pytest.raises(McpToolError, match="无法启动 MCP"):
        OkxMcpReadOnlyClient().list_tools()


def test_mcp_batch_without_response_raises(monkeypatch):
    _patch_popen(monkeypatch, FakeProc(lines=[]))
    with pytest.raises(McpToolError, match="没有返回有效响应"):
        OkxMcpReadOnlyClient(timeout_sec=0.1).list_tools()


def test_mcp_batch_broken_pipe(monkeypatch):
    _patch_popen(monkeypatch, FakeProc(lines=[], write_error=BrokenPipeError("closed")))
    with pytest.raises(McpToolError, match="MCP 写入失败"):
        OkxMcpReadOnlyClient().list_tools()


def test_mcp_rpc_error_field(monkeypatch):
    line = json.dumps({"id": 1, "error": {"message": "boom"}})
    _patch_popen(monkeypatch, FakeProc(lines=[line]))
    with pytest.raises(McpToolError, match="MCP 错误"):
        OkxMcpReadOnlyClient().list_tools()


def test_mcp_rpc_result_not_dict(monkeypatch):
    line = json.dumps({"id": 1, "result": "plain"})
    _patch_popen(monkeypatch, FakeProc(lines=[line]))
    with pytest.raises(McpToolError, match="结果格式异常"):
        OkxMcpReadOnlyClient().list_tools()


def test_mcp_tools_payload_missing_tools_key():
    client = OkxMcpReadOnlyClient(runner=lambda method, params: {"result": 1})
    with pytest.raises(McpToolError, match="tools/list 缺少 tools"):
        client.list_tools()


def test_mcp_runner_exception_becomes_mcp_error():
    client = OkxMcpReadOnlyClient(runner=lambda method, params: RuntimeError("offline"))
    with pytest.raises(McpToolError, match="offline"):
        client.list_tools()


def test_mcp_runner_non_dict_is_wrapped():
    client = OkxMcpReadOnlyClient(runner=lambda method, params: {"tools": []})
    assert client.call_tool("get_ticker", {"instId": "BTC-USDT"}) == {"tools": []}


def _tools(*names: str) -> list[McpTool]:
    return [McpTool(name=n, description="") for n in names]


def test_mcp_snapshot_collects_all_sections():
    def runner(method, params):
        if method == "tools/list":
            return {
                "tools": [
                    {"name": "get_ticker"},
                    {"name": "get_funding_rate"},
                    {"name": "get_open_interest"},
                    {"name": "get_orderbook_depth"},
                    {"name": "get_news"},
                ]
            }
        assert method == "tools/call"
        return {"content": {"ok": params["name"]}}

    snap = OkxMcpReadOnlyClient(runner=runner).snapshot("BTC/USDT")
    assert snap.pair == "BTC/USDT"
    assert snap.sources_ok == ["ticker", "funding_rate", "open_interest", "orderbook_depth", "news"]
    assert snap.errors == []
    payload = snap.to_prompt_payload()
    assert "ticker" in json.dumps(payload, ensure_ascii=False)


def test_mcp_snapshot_records_missing_tools_and_errors():
    calls = {"n": 0}

    def runner(method, params):
        if method == "tools/list":
            return {"tools": [{"name": "get_ticker"}]}
        calls["n"] += 1
        raise McpToolError("端点不可用")

    snap = OkxMcpReadOnlyClient(runner=runner).snapshot("ETH/USDT")
    assert calls["n"] == 1
    assert any("ticker" in e for e in snap.errors)
    assert any("无可用工具" in e for e in snap.errors)


def test_mcp_snapshot_tools_list_failure_returns_empty_snapshot():
    def runner(method, params):
        raise McpToolError("没有工具")

    snap = OkxMcpReadOnlyClient(runner=runner).snapshot("BTC/USDT")
    assert isinstance(snap, MarketSnapshot)
    assert snap.sources_ok == []
    assert snap.errors and snap.errors[0].startswith("tools/list")


def test_mcp_snapshot_accepts_preloaded_tools():
    client = OkxMcpReadOnlyClient(
        runner=lambda method, params: {"content": {"price": "1"}} if method == "tools/call" else {}
    )
    snap = client.snapshot("SOL/USDT", tools=_tools("get_ticker"))
    assert snap.data["ticker"] == {"price": "1"}
    assert len(snap.errors) == 4


def test_mcp_compact_truncates_large_payload():
    big = {"blob": "x" * 5000}
    compacted = mcp_mod._compact(big, max_bytes=100)
    assert compacted["truncated"] is True
    assert len(compacted["preview"]) == 100


def test_mcp_pick_prefers_first_keyword_match():
    tools = _tools("get_orderbook_depth", "get_ticker")
    assert OkxMcpReadOnlyClient._pick(tools, "ticker", "quote").name == "get_ticker"
    assert OkxMcpReadOnlyClient._pick(tools, "missing") is None


# =============================================================== 新闻源


def _news_transport(status=200, payload=None, raise_exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def test_news_disabled_returns_empty():
    assert NewsClient(enabled=False, token="t").fetch("BTC/USDT") == []


def test_news_without_token_returns_empty():
    assert NewsClient(enabled=True, token="").fetch("BTC/USDT") == []


def test_news_parses_items():
    payload = {
        "results": [
            {
                "title": "BTC 突破",
                "published_at": "2026-01-01T00:00:00Z",
                "url": "https://example.test/a",
                "currencies": [{"code": "btc"}, {"nope": 1}, "bad"],
            },
            {"title": "   "},
            {"title": "无时间", "created_at": "2026-01-02T00:00:00Z"},
            "junk",
        ]
    }
    client = NewsClient(enabled=True, token="t", transport=_news_transport(payload=payload))
    items = client.fetch("BTC/USDT")
    assert [i.title for i in items] == ["BTC 突破", "无时间"]
    assert items[0].currencies == ("BTC",)
    assert items[0].url == "https://example.test/a"
    assert items[1].published_at.startswith("2026-01-02")
    assert items[0].to_prompt_payload()["currencies"] == ["BTC"]


def test_news_http_error_returns_empty():
    client = NewsClient(enabled=True, token="t", transport=_news_transport(status=500, payload={}))
    assert client.fetch("BTC/USDT") == []


def test_news_transport_error_returns_empty():
    client = NewsClient(
        enabled=True, token="t", transport=_news_transport(raise_exc=httpx.ConnectError("down"))
    )
    assert client.fetch("BTC/USDT") == []


def test_news_non_dict_payload_returns_empty():
    client = NewsClient(enabled=True, token="t", transport=_news_transport(payload=["x"]))
    assert client.fetch("BTC/USDT") == []
    assert client._parse({"results": "bad"}) == []


# =============================================================== Redis 封装


class FakeRedis:
    def __init__(self, store=None):
        self.store = store if store is not None else {}
        self.ttls = {}
        self.fail = False
        self.ping_result = True

    def _maybe_fail(self):
        if self.fail:
            raise RuntimeError("redis down")

    def get(self, key):
        self._maybe_fail()
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self._maybe_fail()
        self.store[key] = value
        self.ttls[key] = ttl

    def delete(self, key):
        self._maybe_fail()
        self.store.pop(key, None)

    def ping(self):
        self._maybe_fail()
        return self.ping_result


def test_redis_store_get_setex_delete():
    fake = FakeRedis()
    store = RedisStore("redis://x/0", client=fake)
    store.setex("k", 5, "v")
    assert fake.store["k"] == "v"
    assert fake.ttls["k"] == 5
    assert store.get("k") == "v"
    assert store.get("missing") is None
    store.delete("k")
    assert fake.store == {}


def test_redis_store_decodes_bytes():
    fake = FakeRedis(store={"k": b"bytes-value"})
    assert RedisStore("redis://x/0", client=fake).get("k") == "bytes-value"


def test_redis_store_wraps_errors_as_store_error():
    fake = FakeRedis()
    fake.fail = True
    store = RedisStore("redis://x/0", client=fake)
    with pytest.raises(StoreError, match="get 失败"):
        store.get("k")
    with pytest.raises(StoreError, match="setex 失败"):
        store.setex("k", 1, "v")
    with pytest.raises(StoreError, match="delete 失败"):
        store.delete("k")
    assert store.ping() is False


def test_redis_store_passes_through_store_error():
    class Flaky(FakeRedis):
        def get(self, key):
            raise StoreError("already")

    with pytest.raises(StoreError, match="already"):
        RedisStore("redis://x/0", client=Flaky()).get("k")


def test_redis_store_lazy_client(monkeypatch):
    fake = FakeRedis()
    created = {}

    def from_url(url, **kwargs):
        created["url"] = url
        created["kwargs"] = kwargs
        return fake

    monkeypatch.setitem(sys.modules, "redis", types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=from_url)))
    store = RedisStore("redis://127.0.0.1:6379/3", socket_timeout=0.01)
    assert store.client is fake
    assert created["url"] == "redis://127.0.0.1:6379/3"
    assert created["kwargs"]["socket_timeout"] == 0.01
    assert created["kwargs"]["decode_responses"] is True


def test_build_store_variants(monkeypatch):
    monkeypatch.delenv("AIQUANT_STORE", raising=False)
    assert isinstance(build_store("redis://127.0.0.1:6379/0"), RedisStore)
    assert isinstance(build_store("redis://127.0.0.1:6379/0", prefer_memory=True), MemoryStore)
    monkeypatch.setenv("AIQUANT_STORE", "memory")
    assert isinstance(build_store("redis://127.0.0.1:6379/0"), MemoryStore)


# =============================================================== LLM 客户端


def _openai_transport(payload=None, status=200, raise_exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        assert "authorization" in request.headers
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _client(**overrides) -> HttpLLMClient:
    kwargs = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key": "secret-key",
        "base_url": "https://api.test",
    }
    kwargs.update(overrides)
    return HttpLLMClient(**kwargs)


def test_llm_requires_api_key():
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        HttpLLMClient("openai", "m", "", "https://api.test").complete_json("s", "u")


def test_llm_openai_compatible_success():
    payload = {"choices": [{"message": {"content": '{"action":"allow"}'}}]}
    client = _client(transport=_openai_transport(payload=payload))
    assert client.complete_json("sys", "user") == '{"action":"allow"}'


def test_llm_base_url_with_v1_suffix():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    client = _client(base_url="https://api.test/v1", transport=httpx.MockTransport(handler))
    assert client.complete_json("s", "u") == "x"
    assert seen["path"] == "/v1/chat/completions"


def test_llm_openai_content_parts_list():
    payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}, 1]}}]}
    client = _client(transport=_openai_transport(payload=payload))
    assert client.complete_json("s", "u") == "ab"


def test_llm_openai_content_missing_raises():
    payload = {"choices": [{"message": {"content": 5}}]}
    client = _client(transport=_openai_transport(payload=payload))
    with pytest.raises(LLMError, match="响应结构异常"):
        client.complete_json("s", "u")


def test_llm_http_status_error():
    client = _client(transport=_openai_transport(payload={}, status=429))
    with pytest.raises(LLMError, match="LLM 返回 429"):
        client.complete_json("s", "u")


def test_llm_transport_error():
    client = _client(transport=_openai_transport(raise_exc=httpx.ConnectError("down")))
    with pytest.raises(LLMError, match="LLM 请求失败"):
        client.complete_json("s", "u")


def test_llm_invalid_json_body():
    def handler(_request):
        return httpx.Response(200, text="not-json")

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="响应结构异常"):
        client.complete_json("s", "u")


def test_llm_anthropic_success_and_errors():
    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "secret-key"
        assert request.url.path == "/v1/messages"
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "a"}, {"type": "tool_use"}, {"type": "text", "text": "b"}]},
        )

    client = _client(provider="anthropic", transport=httpx.MockTransport(ok_handler))
    assert client.complete_json("s", "u") == "ab"

    bad = _client(provider="anthropic", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(LLMError, match="响应结构异常"):
        bad.complete_json("s", "u")


def test_static_llm_client_exhaustion_and_errors():
    client = StaticLLMClient(["one", RuntimeError("boom")])
    assert client.complete_json("s", "u") == "one"
    with pytest.raises(RuntimeError, match="boom"):
        client.complete_json("s", "u")
    with pytest.raises(LLMError, match="没有更多预置响应"):
        client.complete_json("s", "u")
    assert len(client.calls) == 3


def test_dumps_is_deterministic():
    assert dumps({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'


# =============================================================== 日志脱敏


def _record(msg, args=(), exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, exc_info)


def test_redacting_filter_masks_secret_in_message_and_args():
    flt = RedactingFilter(["super-secret"])
    record = _record("key=%s", ("super-secret",))
    assert flt.filter(record) is True
    assert "super-secret" not in record.getMessage()


def test_redacting_filter_handles_exc_info():
    flt = RedactingFilter(["super-secret"])
    try:
        raise RuntimeError("super-secret leaked")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = _record("boom", (), exc_info)
    flt.filter(record)
    assert record.exc_info is None
    assert "super-secret" not in (record.exc_text or "")


def test_redacting_filter_survives_bad_format():
    flt = RedactingFilter(["s3cret-value"])
    record = _record("value=%d", ("not-an-int",))
    assert flt.filter(record) is True


def test_setup_logging_is_idempotent():
    setup_logging("DEBUG", ["tok-value"])
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
    assert isinstance(root.handlers[0].filters[0], RedactingFilter)
    setup_logging("WARN", [])
    assert len(logging.getLogger().handlers) == 1
    assert get_logger("aiquant.test").name == "aiquant.test"


def test_news_module_reexports():
    assert news_mod.NewsItem(title="t", published_at="2026-01-01T00:00:00Z").source == "cryptopanic"
