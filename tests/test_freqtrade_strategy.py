"""宿主策略测试：指标/信号、闸门接线、``reduce_size`` 缩放、fail-open 兜底。

freqtrade 未安装时策略会退化到占位基类，因此这里可以直接实例化。
"""

from __future__ import annotations

import pandas as pd
import pytest

from aiquant.execution import freqtrade_strategy as fs
from aiquant.gate.decision import (
    RESULT_ALLOW,
    RESULT_BLOCK,
    RESULT_ERROR,
    RESULT_REDUCE,
    Gate,
    GateDecision,
)


class StubGate:
    def __init__(self, allow=True, decision=None, raise_on_entry=None, raise_on_evaluate=None):
        self.allow = allow
        self.decision = decision
        self.raise_on_entry = raise_on_entry
        self.raise_on_evaluate = raise_on_evaluate
        self.calls: list[tuple[str, float]] = []

    def confirm_trade_entry(self, pair: str, amount: float):
        self.calls.append((pair, amount))
        if self.raise_on_entry is not None:
            raise self.raise_on_entry
        return self.allow, self.decision or make_decision(pair)

    def evaluate(self, pair: str, amount: float = 0.0):
        if self.raise_on_evaluate is not None:
            raise self.raise_on_evaluate
        return self.decision or make_decision(pair)


def make_decision(pair, allowed=True, result=RESULT_ALLOW, multiplier=1.0, amount=1.0) -> GateDecision:
    return GateDecision(
        allowed=allowed,
        result=result,
        reason="test",
        pair=pair,
        original_amount=amount,
        adjusted_amount=amount * multiplier,
        size_multiplier=multiplier,
    )


@pytest.fixture
def strategy(monkeypatch):
    gate = StubGate()
    monkeypatch.setattr(fs, "_GATE", gate)
    instance = fs.AiQuantGateStrategy(config={})
    instance._gate = gate
    return instance


def _frame(n=120) -> pd.DataFrame:
    closes = [100 - i for i in range(60)] + [40 + i * 2 for i in range(n - 60)]
    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "volume": [10.0 + (i % 7) for i in range(n)],
        }
    )


def test_populate_indicators_adds_columns(strategy):
    df = strategy.populate_indicators(_frame(), {"pair": "BTC/USDT"})
    for column in ("ema_fast", "ema_slow", "atr", "vol_ma"):
        assert column in df.columns
    assert not df["ema_fast"].isna().all()


def test_entry_and_exit_trends(strategy):
    df = strategy.populate_indicators(_frame(), {})
    df = strategy.populate_entry_trend(df, {})
    df = strategy.populate_exit_trend(df, {})
    assert set(df["enter_long"].unique()) <= {0, 1}
    assert set(df["exit_long"].unique()) <= {0, 1}
    assert df["enter_long"].sum() >= 1
    assert df["exit_long"].sum() >= 1


def test_confirm_trade_entry_uses_gate(strategy):
    assert strategy.confirm_trade_entry("BTC/USDT", "market", 0.01, 100.0, "GTC", None, None, "long") is True
    assert strategy._gate.calls == [("BTC/USDT", 0.01)]


def test_confirm_trade_entry_blocked(strategy):
    strategy._gate.allow = False
    strategy._gate.decision = make_decision("BTC/USDT", allowed=False, result=RESULT_BLOCK)
    assert strategy.confirm_trade_entry("BTC/USDT", "market", 0.01, 100.0, "GTC", None, None, "long") is False


def test_confirm_trade_entry_fail_open_on_exception(strategy):
    strategy._gate.raise_on_entry = RuntimeError("redis gone")
    assert strategy.confirm_trade_entry("ETH/USDT", "market", 0.02, 10.0, "GTC", None, None, "long") is True


def test_decide_returns_error_decision_on_exception(strategy):
    strategy._gate.raise_on_entry = ValueError("bad")
    allow, decision = strategy._decide("BTC/USDT", 1.5)
    assert allow is True
    assert decision.result == RESULT_ERROR
    assert decision.original_amount == 1.5


def test_custom_stake_amount_scales_on_reduce(strategy):
    strategy._gate.decision = make_decision("BTC/USDT", result=RESULT_REDUCE, multiplier=0.5)
    stake = strategy.custom_stake_amount("BTC/USDT", None, 100.0, 200.0, 10.0, 1000.0, 1.0, None, "long")
    assert stake == 100.0


def test_custom_stake_amount_respects_min_stake_floor(strategy):
    strategy._gate.decision = make_decision("BTC/USDT", result=RESULT_REDUCE, multiplier=0.01)
    stake = strategy.custom_stake_amount("BTC/USDT", None, 100.0, 200.0, 50.0, 1000.0, 1.0, None, "long")
    assert stake == 200.0


def test_custom_stake_amount_passthrough(strategy):
    assert strategy.custom_stake_amount("BTC/USDT", None, 1.0, 150.0, None, 1000.0, 1.0, None, "long") == 150.0


def test_custom_stake_amount_survives_gate_exception(strategy):
    strategy._gate.raise_on_evaluate = RuntimeError("boom")
    assert strategy.custom_stake_amount("BTC/USDT", None, 1.0, 150.0, None, 1000.0, 1.0, None, "long") == 150.0


def test_confirm_trade_exit_always_allowed(strategy):
    assert strategy.confirm_trade_exit("BTC/USDT", object(), "market", 1.0, 100.0, "GTC", "roi", None) is True


def test_get_gate_is_cached(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(fs, "_GATE", None)
    monkeypatch.setattr(fs, "_build_gate", lambda: sentinel)
    assert fs.get_gate() is sentinel
    assert fs.get_gate() is sentinel
    monkeypatch.setattr(fs, "_GATE", None)


def test_build_gate_from_settings(monkeypatch, settings_factory):
    from aiquant.label_service.store.memory_store import MemoryStore

    settings = settings_factory()
    monkeypatch.setattr(fs, "get_settings", lambda: settings)
    monkeypatch.setattr(fs, "build_store", lambda _url: MemoryStore())
    gate = fs._build_gate()
    assert isinstance(gate, Gate)
    assert gate.fail_open is True
    assert gate.min_amounts["BTC/USDT"] == pytest.approx(0.001)


def test_module_exports():
    assert "AiQuantGateStrategy" in fs.__all__
