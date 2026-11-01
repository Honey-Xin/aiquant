"""只读自检测试（G-06：暴露下单工具必须拒绝启动）。"""

from __future__ import annotations

import pytest

from aiquant.label_service.readonly_check import (
    ReadonlyCheckResult,
    cmd_is_readonly,
    is_mutating_tool_name,
    run_readonly_check,
)
from aiquant.label_service.sources.okx_mcp import McpTool, McpToolError


class TestMutatingNames:
    @pytest.mark.parametrize(
        "name",
        [
            "place_order",
            "create_order",
            "cancel_order",
            "trade_place_order",
            "account_set_leverage",
            "withdraw",
            "transfer_funds",
            "close_position",
            "amend_order",
            "set_position_mode",
            "okx_trade_order",
        ],
    )
    def test_mutating(self, name):
        assert is_mutating_tool_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "get_ticker",
            "get_funding_rate",
            "list_instruments",
            "get_orderbook",
            "market_get_candles",
            "get_balance",
            "unsubscribe_candles",
            "search_news",
            "",
        ],
    )
    def test_read_only(self, name):
        assert is_mutating_tool_name(name) is False


class TestCmdReadonly:
    def test_with_flag(self):
        assert cmd_is_readonly("okx-trade-mcp --read-only") is True
        assert cmd_is_readonly('"okx-trade-mcp" --profile default --read-only') is True

    def test_without_flag(self):
        assert cmd_is_readonly("okx-trade-mcp") is False
        assert cmd_is_readonly("okx-trade-mcp --profile default") is False


def tools(*names):
    return [McpTool(name=n, description="") for n in names]


class TestRunReadonlyCheck:
    def test_g06_rejects_command_without_flag(self):
        result = run_readonly_check("okx-trade-mcp", lister=lambda: tools("get_ticker"))
        assert result.ok is False
        assert result.status == "cmd_not_readonly"
        assert "--read-only" in result.detail

    def test_g06_rejects_exposed_mutating_tool(self):
        result = run_readonly_check(
            "okx-trade-mcp --read-only",
            lister=lambda: tools("get_ticker", "place_order"),
        )
        assert result.ok is False
        assert result.status == "tools_exposed"
        assert result.forbidden == ["place_order"]

    def test_ok_when_only_readonly_tools(self):
        result = run_readonly_check(
            "okx-trade-mcp --read-only",
            lister=lambda: tools("get_ticker", "get_funding_rate"),
        )
        assert result.ok is True
        assert result.status == "ok"
        assert result.tools == ["get_ticker", "get_funding_rate"]

    def test_unavailable_blocks_startup_when_required(self):
        def boom():
            raise McpToolError("connect failed")

        result = run_readonly_check("okx-trade-mcp --read-only", lister=boom, required=True)
        assert result.ok is False
        assert result.status == "unavailable"

    def test_unavailable_allowed_when_not_required(self):
        def boom():
            raise OSError("no binary")

        result = run_readonly_check("okx-trade-mcp --read-only", lister=boom, required=False)
        assert result.ok is True
        assert result.status == "unavailable"

    def test_result_to_dict(self):
        payload = ReadonlyCheckResult(ok=True, status="ok", tools=["get_ticker"], detail="1 个只读工具").to_dict()
        assert payload["status"] == "ok"
        assert payload["forbidden"] == []
