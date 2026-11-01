"""Freqtrade 宿主策略：唯一交易执行路径（FR-2.1 / FR-2.2）。

* ``confirm_trade_entry`` —— 读标签裁决是否允许开新仓；``reduce_size`` 通过
  ``custom_stake_amount`` 缩放下注；
* ``confirm_trade_exit`` —— 平仓永远允许（守门只管"新仓"）；
* 绝不在此处调用 LLM，也绝不访问网络（除 Redis），保证实盘路径无同步 LLM 调用；
* 策略本身不产生交易信号时会退化为持仓不动，因此附带一个**参考基线**信号
  （EMA 交叉 + 趋势过滤），生产请替换为自己验证过的策略，闸门层无需改动。

部署：``freqtrade trade --strategy AiQuantGateStrategy --config config/freqtrade/config.json``
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

from aiquant.audit.logger import AuditLog
from aiquant.gate.decision import RESULT_REDUCE, Gate, GateDecision, GateToggle
from aiquant.label_service.store import build_store
from aiquant.logging_setup import get_logger, setup_logging
from aiquant.settings import get_settings

try:  # pragma: no cover - 运行时由 freqtrade 提供
    from freqtrade.strategy import IStrategy
except Exception:  # pragma: no cover - 允许在无 freqtrade 的环境导入做单测
    class IStrategy:  # type: ignore[no-redef]
        """最小占位基类。"""

        timeframe = "5m"

        def __init__(self, config: dict[str, Any] | None = None) -> None:
            self.config = config or {}


log = get_logger(__name__)

_GATE: Gate | None = None


def _build_gate() -> Gate:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secrets())
    store = build_store(settings.redis_url)
    audit = AuditLog(settings.audit_log_dir, settings.audit_retention_days, settings.secrets())
    toggle = GateToggle(store=store, default=settings.gate_enabled, audit=audit)
    return Gate(
        store=store,
        toggle=toggle,
        audit=audit,
        min_amounts=settings.min_amounts,
        fail_open=settings.gate_fail_open,
    )


def get_gate() -> Gate:
    global _GATE
    if _GATE is None:
        _GATE = _build_gate()
    return _GATE


class AiQuantGateStrategy(IStrategy):
    """宿主策略：信号 + 闸门裁决。"""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False
    startup_candle_count = 60

    minimal_roi = {"0": 0.02, "240": 0.01, "720": 0.005}
    stoploss = -0.03

    # 由 config.json 的 protections 承担全局风控；这里只留兜底
    trailing_stop = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._gate = get_gate()

    # ------------------------------------------------------------- 指标
    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict[str, Any]) -> pd.DataFrame:
        df = dataframe
        df["ema_fast"] = df["close"].ewm(span=12, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=48, adjust=False).mean()
        df["atr"] = (df["high"] - df["low"]).rolling(14).mean()
        df["vol_ma"] = df["volume"].rolling(24).mean()
        return df

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict[str, Any]) -> pd.DataFrame:
        df = dataframe
        df["enter_long"] = 0
        crossed = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        liquid = df["volume"] > df["vol_ma"].fillna(0)
        df.loc[crossed & liquid, "enter_long"] = 1
        return df

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict[str, Any]) -> pd.DataFrame:
        df = dataframe
        df["exit_long"] = 0
        crossed_down = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        df.loc[crossed_down, "exit_long"] = 1
        return df

    # ------------------------------------------------------------- 闸门
    def _decide(self, pair: str, amount: float) -> tuple[bool, GateDecision]:
        try:
            return self._gate.confirm_trade_entry(pair, float(amount))
        except Exception as exc:  # noqa: BLE001 - 异常一律 fail-open 且审计
            log.error("闸门异常，按 GATE_FAIL_OPEN 处理: %s", type(exc).__name__)
            return True, GateDecision(
                allowed=True,
                result="error",
                reason=f"gate exception: {type(exc).__name__}",
                pair=pair,
                original_amount=float(amount),
                adjusted_amount=float(amount),
            )

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: Any,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        allow, decision = self._decide(pair, amount)
        log.info(
            "confirm_trade_entry pair=%s side=%s amount=%s result=%s",
            pair,
            side,
            amount,
            decision.result,
        )
        return bool(allow)

    def custom_stake_amount(
        self,
        pair: str,
        current_time: Any,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        """``reduce_size`` 时按乘数缩放下注；其余情况原样返回。"""
        try:
            decision = self._gate.evaluate(pair, amount=proposed_stake)
        except Exception:  # noqa: BLE001
            return proposed_stake
        if decision.result == RESULT_REDUCE:
            scaled = proposed_stake * decision.size_multiplier
            floor = min_stake or 0.0
            return max(scaled, floor) if scaled >= floor else proposed_stake
        return proposed_stake

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Any,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: Any,
        **kwargs: Any,
    ) -> bool:
        # 平仓永远允许：守门只拦截"开新仓"
        log.info("confirm_trade_exit pair=%s reason=%s -> allow", pair, exit_reason)
        return True


__all__ = ["AiQuantGateStrategy", "get_gate", "os"]
