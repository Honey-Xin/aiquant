"""okx-trade-cli 适配器测试（G-09：CLI 超时不得静默重下单）。"""

from __future__ import annotations

import subprocess

import pytest

from aiquant import metrics as M
from aiquant.audit.logger import AuditLog
from aiquant.execution.okx_cli import DEFAULT_TEMPLATE, ExecResult, OkxCliExecutor


class FakeRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, cmd, timeout):
        self.calls.append(list(cmd))
        self.timeouts.append(timeout)
        item = self.results.pop(0) if self.results else subprocess.CompletedProcess(cmd, 0, "", "")
        if isinstance(item, Exception):
            raise item
        return item


def ok(cmd, stdout='{"ordId": "abc", "sCode": "0"}', code=0, stderr=""):
    return subprocess.CompletedProcess(cmd, code, stdout, stderr)


def build(tmp_path, *results, **kwargs):
    runner = FakeRunner(*results)
    audit = AuditLog(tmp_path / "logs", 180, ())
    execu = OkxCliExecutor(audit=audit, runner=runner, retries=kwargs.pop("retries", 0), **kwargs)
    return execu, runner, audit


class TestBuildCommand:
    def test_market_order_default_template(self, tmp_path):
        ex, _, _ = build(tmp_path)
        cmd = ex.build_command("market-buy", "BTC/USDT", "buy", 0.01)
        assert cmd[0] == "okx-trade-cli"
        assert "--json" in cmd
        assert cmd[cmd.index("--inst-id") + 1] == "BTC/USDT"
        assert cmd[cmd.index("--side") + 1] == "buy"
        assert cmd[cmd.index("--size") + 1] == "0.01"
        assert "--px" not in cmd

    def test_limit_order_includes_price(self, tmp_path):
        ex, _, _ = build(tmp_path)
        cmd = ex.build_command("limit-buy", "ETH/USDT", "buy", 1.5, price=2500.25)
        assert cmd[cmd.index("--px") + 1] == "2500.25"

    def test_extra_args_quoted(self, tmp_path):
        ex, _, _ = build(tmp_path, extra_args=["--td-mode", "cross", "--tag", "ai quant"])
        cmd = ex.build_command("market-buy", "BTC/USDT", "buy", 1)
        assert "--td-mode" in cmd and "cross" in cmd
        assert "ai quant" in cmd

    def test_default_template_has_placeholders(self):
        for key in ("{cli}", "{action}", "{pair}", "{side}", "{amount}", "{price_part}"):
            assert key in DEFAULT_TEMPLATE


class TestExecute:
    def test_success(self, tmp_path):
        ex, runner, audit = build(tmp_path, ok(None))
        result = ex.market_buy("BTC/USDT", 0.01)
        assert result.ok is True
        assert result.code == 0
        assert result.attempts == 1
        assert result.latency_ms >= 0
        assert "ordId" in result.stdout
        assert len(runner.calls) == 1
        records = [r for r in audit.iter_records(stream="execution") if r["event"] == "cli_call"]
        assert records and records[-1]["pair"] == "BTC/USDT"

    def test_limit_sell_price(self, tmp_path):
        ex, runner, _ = build(tmp_path, ok(None))
        result = ex.limit_sell("ETH/USDT", 2.0, 3100.0)
        assert result.side == "sell"
        assert runner.calls[0][runner.calls[0].index("--px") + 1] == "3100.0"

    def test_failure_not_retried(self, tmp_path):
        ex, runner, audit = build(tmp_path, ok(None, code=1, stderr="insufficient balance"), retries=1)
        result = ex.market_sell("BTC/USDT", 0.01)
        assert result.ok is False
        assert result.code == 1
        assert result.attempts == 1
        assert len(runner.calls) == 1
        assert result.error == "exit 1"
        assert any(r["event"] == "cli_error" for r in audit.iter_records(stream="execution"))

    def test_g09_timeout_returns_error_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr("aiquant.execution.okx_cli.time.sleep", lambda *_: None)
        timeout_exc = subprocess.TimeoutExpired(cmd=["okx-trade-cli"], timeout=10.0)
        ex, runner, audit = build(tmp_path, timeout_exc, timeout_exc, retries=1)
        result = ex.market_buy("BTC/USDT", 0.01)
        assert result.ok is False
        assert result.error == "timeout"
        assert result.attempts == 2  # 超时会重试，但重试次数受配置上限约束
        assert len(runner.calls) == 2
        assert any(r["event"] == "cli_error" for r in audit.iter_records(stream="execution"))

    def test_timeout_then_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("aiquant.execution.okx_cli.time.sleep", lambda *_: None)
        ex, runner, _ = build(tmp_path, subprocess.TimeoutExpired(["cli"], 1.0), ok(None), retries=1)
        result = ex.market_buy("BTC/USDT", 0.01)
        assert result.ok is True
        assert result.attempts == 2

    def test_server_error_retried(self, tmp_path, monkeypatch):
        monkeypatch.setattr("aiquant.execution.okx_cli.time.sleep", lambda *_: None)
        ex, runner, _ = build(tmp_path, ok(None, code=502), ok(None), retries=1)
        result = ex.market_buy("BTC/USDT", 0.01)
        assert result.ok is True
        assert result.attempts == 2

    def test_oserror(self, tmp_path):
        ex, _, _ = build(tmp_path, FileNotFoundError("okx-trade-cli"))
        result = ex.market_buy("BTC/USDT", 0.01)
        assert result.ok is False
        assert result.error == "oserror: FileNotFoundError"

    def test_metrics_recorded(self, tmp_path):
        ex, _, _ = build(tmp_path, ok(None))
        before = M.CLI_CALLS.value(result="ok")
        ex.market_buy("BTC/USDT", 0.01)
        assert M.CLI_CALLS.value(result="ok") == before + 1

    def test_stdout_truncated(self, tmp_path):
        ex, _, _ = build(tmp_path, ok(None, stdout="x" * 5000))
        assert len(ex.market_buy("BTC/USDT", 1).stdout) == 2000

    def test_timeout_passed_to_runner(self, tmp_path):
        ex, runner, _ = build(tmp_path, ok(None), timeout_sec=1.25)
        ex.market_buy("BTC/USDT", 1)
        assert runner.timeouts == [1.25]

    def test_to_dict_has_no_secrets(self, tmp_path):
        ex, _, _ = build(tmp_path, ok(None))
        payload = ex.market_buy("BTC/USDT", 0.01).to_dict()
        assert "cmd" not in payload
        assert payload["order_id"] is None

    def test_exec_result_defaults(self):
        result = ExecResult(ok=True, action="market-buy", pair="BTC/USDT", side="buy", amount=1.0)
        assert result.code == 0 and result.attempts == 0 and result.cmd == []
