"""OKX 只读 MCP 数据源（``okx-trade-mcp --read-only``）。

通过 stdio JSON-RPC 与 MCP server 通信：initialize → tools/list → tools/call。
只读自检会在这里拿到 tools/list 的结果，执行域与标签域共用同一个客户端实现。
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from aiquant.logging_setup import get_logger

log = get_logger(__name__)


class McpToolError(RuntimeError):
    pass


@dataclass
class McpTool:
    name: str
    description: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "McpTool":
        return cls(name=str(payload.get("name", "")), description=str(payload.get("description", "")))


@dataclass
class MarketSnapshot:
    pair: str
    ts: str
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    sources_ok: list[str] = field(default_factory=list)

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "ts": self.ts,
            "data": self.data,
            "sources_ok": self.sources_ok,
            "errors": self.errors,
        }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OkxMcpReadOnlyClient:
    """只读 MCP 客户端。

    ``runner`` 可注入：签名为 ``runner(method, params) -> dict``，用于单测与离线回放，
    避免测试依赖真实 OKX 连接。
    """

    def __init__(
        self,
        cmd: str = "okx-trade-mcp --read-only",
        timeout_sec: float = 15.0,
        runner: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.cmd = cmd
        self.timeout_sec = float(timeout_sec)
        self._runner = runner
        self._next_id = 0

    # ------------------------------------------------------------ 传输层
    def _batch(self, requests: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        argv = shlex.split(self.cmd, posix=False)
        try:
            proc = subprocess.Popen(  # noqa: S603 - 命令来自可信配置
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except (OSError, FileNotFoundError) as exc:
            raise McpToolError(f"无法启动 MCP: {type(exc).__name__}: {exc}") from exc

        responses: list[dict[str, Any]] = []
        expected = {int(r["id"]) for r in requests}

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    responses.append(msg)
                    if expected and not (expected - {int(r.get("id", -1)) for r in responses}):
                        break

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()

        try:
            assert proc.stdin is not None
            for req in requests:
                proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpToolError(f"MCP 写入失败: {exc}") from exc

        thread.join(self.timeout_sec)

        stderr_tail = ""
        try:
            proc.terminate()
            _, err = proc.communicate(timeout=2)
            stderr_tail = (err or "").strip()[-400:]
        except Exception:  # noqa: BLE001 - 收尾失败不影响主流程
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

        if not responses:
            detail = f"stderr={stderr_tail}" if stderr_tail else "无响应"
            raise McpToolError(f"MCP 在 {self.timeout_sec}s 内没有返回有效响应（{detail}）")

        out: dict[int, dict[str, Any]] = {}
        for msg in responses:
            try:
                out[int(msg["id"])] = msg
            except (KeyError, TypeError, ValueError):
                continue
        if not out:
            raise McpToolError("MCP 响应缺少 id")
        return out

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._runner is not None:
            result = self._runner(method, params or {})
            if isinstance(result, Exception):
                raise McpToolError(str(result))
            return result if isinstance(result, dict) else {"result": result}
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        resp = self._batch([req]).get(self._next_id, {})
        if "error" in resp:
            raise McpToolError(f"MCP 错误: {resp['error']}")
        result = resp.get("result")
        if not isinstance(result, dict):
            raise McpToolError("MCP 结果格式异常")
        return result

    # ------------------------------------------------------------ MCP 能力
    def list_tools(self) -> list[McpTool]:
        result = self._rpc("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpToolError("tools/list 缺少 tools 字段")
        return [McpTool.from_payload(t) for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return result

    # ------------------------------------------------------------ 业务读数
    @staticmethod
    def _pick(tools: list[McpTool], *keywords: str) -> McpTool | None:
        for kw in keywords:
            for tool in tools:
                if kw in tool.name.lower():
                    return tool
        return None

    def snapshot(self, pair: str, tools: list[McpTool] | None = None) -> MarketSnapshot:
        """采集一个交易对的只读快照。任一子项失败只记录错误，不抛异常。"""
        snap = MarketSnapshot(pair=pair, ts=_utc_iso())
        inst = pair.replace("/", "-")
        try:
            available = tools if tools is not None else self.list_tools()
        except McpToolError as exc:
            snap.errors.append(f"tools/list: {exc}")
            return snap

        plan: list[tuple[str, tuple[str, ...], dict[str, Any]]] = [
            ("ticker", ("ticker", "quote", "price"), {"instId": inst}),
            ("funding_rate", ("funding",), {"instId": inst}),
            ("open_interest", ("open_interest", "openinterest", "oi"), {"instId": inst}),
            ("orderbook_depth", ("depth", "orderbook", "book"), {"instId": inst}),
            ("news", ("news", "announcement"), {"instId": inst}),
        ]
        for label, keywords, args in plan:
            tool = self._pick(available, *keywords)
            if tool is None:
                snap.errors.append(f"{label}: 无可用工具")
                continue
            try:
                result = self.call_tool(tool.name, args)
            except McpToolError as exc:
                snap.errors.append(f"{label}: {exc}")
                continue
            content = result.get("content", result)
            snap.data[label] = _compact(content)
            snap.sources_ok.append(label)
        return snap


def _compact(value: Any, max_bytes: int = 4000) -> Any:
    """压缩工具返回：控制 prompt 体积，避免把整本 orderbook 塞进上下文。"""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_bytes:
        return value
    return {"truncated": True, "preview": text[:max_bytes]}
