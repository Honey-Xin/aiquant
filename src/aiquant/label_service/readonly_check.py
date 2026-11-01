"""只读自检（FR-1.4 / G-06）。

启动时：
  1. 校验启动命令确实带 ``--read-only``；
  2. 拉取 MCP 的 tools/list；
  3. 只要出现任何一个"会改状态"的工具，标签服务必须拒绝启动；
  4. 自检结果写入审计日志（``readonly_check`` 事件）。

MCP 二进制缺失时不算"暴露下单工具"，但默认按 ``READONLY_CHECK_REQUIRED=true``
视为自检未通过；本地开发可显式关闭。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from aiquant.logging_setup import get_logger

from .sources.okx_mcp import McpTool, McpToolError, OkxMcpReadOnlyClient

log = get_logger(__name__)

# 明确的下单/资金类工具名（规范化后精确匹配）
EXPLICIT_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "placeorder",
        "createorder",
        "submitorder",
        "amendorder",
        "cancelorder",
        "cancelallorders",
        "closeposition",
        "closeallpositions",
        "setleverage",
        "setmarginmode",
        "setpositionmode",
        "withdraw",
        "transfer",
        "createalgoorder",
        "createalgoorder",
        "createorderprecheck",
        "order",
        "trade",
        "buy",
        "sell",
    }
)

# 以这些动词开头的工具一定在改状态
MUTATING_VERBS: frozenset[str] = frozenset(
    {
        "place",
        "create",
        "submit",
        "amend",
        "modify",
        "cancel",
        "close",
        "withdraw",
        "transfer",
        "set",
        "delete",
        "remove",
        "buy",
        "sell",
        "patch",
        "update",
        "reduce",
        "add",
    }
)

# 只读动词白名单：即便名字里带 order/position 也不触发拦截
READ_ONLY_VERBS: frozenset[str] = frozenset(
    {
        "get",
        "list",
        "fetch",
        "query",
        "read",
        "describe",
        "info",
        "search",
        "watch",
        "subscribe",
        "latest",
    }
)

_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _tokens(name: str) -> list[str]:
    parts = [p.lower() for p in _SPLIT_RE.split(name) if p]
    if len(parts) == 1:
        # camelCase / snake 混合名兜底拆词
        camel = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)
        if len(camel) > 1:
            parts = [p.lower() for p in camel]
    return parts


def is_mutating_tool_name(name: str) -> bool:
    """判断工具名是否属于"会改状态"的一类。"""
    if not name:
        return False
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    if normalized in EXPLICIT_MUTATING_TOOLS:
        return True
    toks = _tokens(name)
    if not toks:
        return False
    first = toks[0]
    if first in READ_ONLY_VERBS:
        return False
    if first in MUTATING_VERBS:
        return True
    # "trade_place" / "okx_trade_order" 这类组合：首词是命名空间，尾词决定语义
    return any(t in MUTATING_VERBS or t in EXPLICIT_MUTATING_TOOLS for t in toks[1:]) and first in {
        "trade",
        "okx",
        "account",
        "spot",
        "swap",
        "futures",
    }


@dataclass
class ReadonlyCheckResult:
    ok: bool
    status: str  # ok | tools_exposed | unavailable | cmd_not_readonly | error
    tools: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "tools": list(self.tools),
            "forbidden": list(self.forbidden),
            "detail": self.detail,
        }


def cmd_is_readonly(cmd: str) -> bool:
    """启动命令必须显式带 --read-only。"""
    try:
        argv = shlex.split(cmd, posix=False)
    except ValueError:
        argv = cmd.split()
    return any(a.strip("\"'") == "--read-only" for a in argv)


def run_readonly_check(
    cmd: str,
    timeout_sec: float = 15.0,
    required: bool = True,
    lister=None,
) -> ReadonlyCheckResult:
    """执行自检。``lister`` 可注入（返回 list[McpTool]）以避免测试依赖真实 MCP。"""
    if not cmd_is_readonly(cmd):
        return ReadonlyCheckResult(
            ok=False,
            status="cmd_not_readonly",
            detail=f"启动命令缺少 --read-only: {cmd}",
        )

    if lister is None:
        client = OkxMcpReadOnlyClient(cmd=cmd, timeout_sec=timeout_sec)
        lister = client.list_tools

    try:
        tools: list[McpTool] = lister()
    except (McpToolError, OSError, Exception) as exc:  # noqa: BLE001 - 自检不得崩
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("只读自检无法获取工具列表: %s", detail)
        return ReadonlyCheckResult(
            ok=not required,
            status="unavailable",
            detail=detail,
        )

    names = [t.name for t in tools if t.name]
    forbidden = [n for n in names if is_mutating_tool_name(n)]
    if forbidden:
        return ReadonlyCheckResult(
            ok=False,
            status="tools_exposed",
            tools=names,
            forbidden=forbidden,
            detail="MCP 暴露了改状态的工具，标签服务必须拒绝启动",
        )
    return ReadonlyCheckResult(ok=True, status="ok", tools=names, detail=f"{len(names)} 个只读工具")
