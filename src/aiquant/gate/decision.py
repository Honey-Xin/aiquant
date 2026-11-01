"""闸门裁决核心（FR-2.2）。

这一段是整个系统最关键的确定性逻辑，必须满足：
  * 纯读：只读一次 Redis，不发任何网络请求，不调用 LLM；
  * 五条路径顺序严格：missing → block → reduce → allow → error；
  * 任何异常都返回 True（fail-open），绝不因为标签问题阻塞下单。

实现刻意做成"函数 + 不可变结果"，便于单测与在 Freqtrade 中复用。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from aiquant import metrics as M
from aiquant.audit.logger import AuditLog
from aiquant.logging_setup import get_logger

from ..label_service.schema import Label
from ..label_service.store.base import LabelStore, StoreError

log = get_logger(__name__)

LABEL_KEY_TEMPLATE = "ai_gate:{pair}"
GATE_ENABLED_KEY = "system:gate_enabled"
HALT_KEY = "system:halt"

RESULT_ALLOW = "allow"
RESULT_MISSING = "missing"
RESULT_BLOCK = "block"
RESULT_REDUCE = "reduce"
RESULT_ERROR = "error"
RESULT_DISABLED = "disabled"


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    result: str
    reason: str
    pair: str
    original_amount: float
    adjusted_amount: float
    size_multiplier: float = 1.0
    label: dict | None = None
    latency_ms: float = 0.0
    meta: Mapping[str, object] = field(default_factory=dict)

    def to_audit(self) -> dict:
        return {
            "pair": self.pair,
            "result": self.result,
            "allowed": self.allowed,
            "reason": self.reason,
            "original_amount": self.original_amount,
            "adjusted_amount": self.adjusted_amount,
            "size_multiplier": self.size_multiplier,
            "label": self.label,
            "latency_ms": round(self.latency_ms, 4),
        }


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _record(result: str) -> None:
    M.GATE_TOTAL.inc(result=result)


class GateToggle:
    """闸门开关：Redis 覆盖 > 环境变量 > 默认值，热生效且变更写审计（FR-2.6）。"""

    def __init__(
        self,
        store: LabelStore | None = None,
        default: bool = True,
        audit: AuditLog | None = None,
    ) -> None:
        self.store = store
        self.default = default
        self.audit = audit
        self._last_state: bool | None = None

    def _from_store(self) -> bool | None:
        if self.store is None:
            return None
        try:
            raw = self.store.get(GATE_ENABLED_KEY)
        except StoreError:
            return None
        if raw is None:
            return None
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _from_env(self) -> bool | None:
        raw = os.environ.get("GATE_ENABLED")
        if raw is None:
            return None
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def is_enabled(self) -> bool:
        value = self._from_store()
        if value is None:
            value = self._from_env()
        if value is None:
            value = self.default
        M.GATE_ENABLED_GAUGE.set(1.0 if value else 0.0)
        if self._last_state is not None and self._last_state != value and self.audit is not None:
            self.audit.write(
                "gate",
                "gate_toggle_changed",
                enabled=value,
                previous=self._last_state,
            )
            log.warning("闸门开关变更: %s -> %s", self._last_state, value)
        self._last_state = value
        return value


class Gate:
    """执行域闸门：只读标签 + 确定性裁决 + 审计。"""

    def __init__(
        self,
        store: LabelStore,
        toggle: GateToggle | None = None,
        audit: AuditLog | None = None,
        min_amounts: Mapping[str, float] | None = None,
        fail_open: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.audit = audit or AuditLog()
        self.toggle = toggle or GateToggle(store=store, audit=self.audit)
        self.min_amounts = dict(min_amounts or {})
        self.fail_open = bool(fail_open)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------- 最小下单量
    def min_amount(self, pair: str) -> float:
        return float(self.min_amounts.get(pair, 0.0))

    # ------------------------------------------------------------- 读标签
    def read_label(self, pair: str) -> tuple[Label | None, str | None]:
        """返回 (label, error)。存储故障返回 (None, error)。"""
        key = LABEL_KEY_TEMPLATE.format(pair=pair)
        try:
            raw = self.store.get(key)
        except StoreError as exc:
            return None, f"store_error: {exc}"
        if raw is None:
            return None, None
        try:
            return Label.from_json(raw), None
        except Exception as exc:  # noqa: BLE001 - 非法 JSON/schema 都算 error 路径
            return None, f"parse_error: {exc}"

    # ------------------------------------------------------------- 裁决
    def evaluate(self, pair: str, amount: float, now: datetime | None = None) -> GateDecision:
        started = time.perf_counter()
        decision = self._evaluate(pair, float(amount), started, now)
        elapsed = time.perf_counter() - started
        M.GATE_DECISION_SECONDS.observe(elapsed)
        _record(decision.result)
        self.audit.write("gate", "gate_decision", **decision.to_audit())
        return GateDecision(**{**decision.__dict__, "latency_ms": elapsed * 1000.0})

    def _evaluate(self, pair: str, amount: float, started: float, now: datetime | None) -> GateDecision:
        # 1) 开关：关闭时行为等同纯策略
        if not self.toggle.is_enabled():
            return GateDecision(
                allowed=True,
                result=RESULT_DISABLED,
                reason="gate disabled",
                pair=pair,
                original_amount=amount,
                adjusted_amount=amount,
            )

        ts = _now(now if now is not None else self._clock())

        # 2) 读标签：缺失 / 解析失败 / 过期 → 默认放行
        label, error = self.read_label(pair)
        if error is not None:
            return self._fail_open_or_closed(
                pair, amount, RESULT_ERROR, f"label error: {error}"
            )
        if label is None:
            return self._fail_open_or_closed(pair, amount, RESULT_MISSING, "label missing")
        if label.is_expired(ts):
            return self._fail_open_or_closed(
                pair, amount, RESULT_MISSING, "label expired"
            )

        snapshot = _label_snapshot(label)

        # 3) block_new_entries → 拒绝
        if label.action == "block_new_entries":
            return GateDecision(
                allowed=False,
                result=RESULT_BLOCK,
                reason=f"label action=block_new_entries regime={label.regime}",
                pair=pair,
                original_amount=amount,
                adjusted_amount=0.0,
                size_multiplier=0.0,
                label=snapshot,
            )

        # 4) reduce_size → 乘数降仓；低于最小下单量则拒绝
        if label.action == "reduce_size":
            multiplier = float(label.size_multiplier)
            adjusted = amount * multiplier
            floor = self.min_amount(pair)
            if adjusted <= 0 or (floor > 0 and adjusted < floor):
                return GateDecision(
                    allowed=False,
                    result=RESULT_REDUCE,
                    reason=(
                        f"reduce_size 后 {adjusted:.8f} 低于最小下单量 {floor:.8f}"
                        f"（multiplier={multiplier}）"
                    ),
                    pair=pair,
                    original_amount=amount,
                    adjusted_amount=adjusted,
                    size_multiplier=multiplier,
                    label=snapshot,
                )
            return GateDecision(
                allowed=True,
                result=RESULT_REDUCE,
                reason=f"reduce_size multiplier={multiplier}",
                pair=pair,
                original_amount=amount,
                adjusted_amount=adjusted,
                size_multiplier=multiplier,
                label=snapshot,
            )

        # 5) allow / 未知值 → 放行
        return GateDecision(
            allowed=True,
            result=RESULT_ALLOW,
            reason=f"label action={label.action}",
            pair=pair,
            original_amount=amount,
            adjusted_amount=amount,
            size_multiplier=1.0,
            label=snapshot,
        )

    # ------------------------------------------------------------- 降级
    def _fail_open_or_closed(self, pair: str, amount: float, result: str, reason: str) -> GateDecision:
        if self.fail_open:
            return GateDecision(
                allowed=True,
                result=result,
                reason=reason,
                pair=pair,
                original_amount=amount,
                adjusted_amount=amount,
            )
        return GateDecision(
            allowed=False,
            result=result,
            reason=f"{reason} (fail-closed)",
            pair=pair,
            original_amount=amount,
            adjusted_amount=0.0,
        )

    # ------------------------------------------------------------- Freqtrade 钩子
    def confirm_trade_entry(self, pair: str, amount: float) -> tuple[bool, GateDecision]:
        try:
            decision = self.evaluate(pair, amount)
        except Exception as exc:  # noqa: BLE001 - 任何未捕获异常都必须放行
            log.error("闸门异常，按 fail-open 放行: %s", type(exc).__name__)
            _record(RESULT_ERROR)
            fallback = GateDecision(
                allowed=True,
                result=RESULT_ERROR,
                reason=f"gate exception: {type(exc).__name__}",
                pair=pair,
                original_amount=float(amount),
                adjusted_amount=float(amount),
            )
            self.audit.write("gate", "gate_decision", **fallback.to_audit())
            return True, fallback
        return decision.allowed, decision

    def size_multiplier(self, pair: str) -> float:
        """给 ``custom_stake_amount`` 用：返回仓位乘数（1.0 表示不变）。"""
        try:
            decision = self.evaluate(pair, 1.0)
        except Exception:  # noqa: BLE001
            return 1.0
        if not decision.allowed:
            return 0.0
        return float(decision.size_multiplier or 1.0)

    # ------------------------------------------------------------- halt
    def halt_active(self) -> bool:
        try:
            raw = self.store.get(HALT_KEY)
        except StoreError:
            return False
        active = raw is not None and str(raw).strip() in {"1", "true", "True"}
        M.SYSTEM_HALT.set(1.0 if active else 0.0)
        return active


def _label_snapshot(label: Label) -> dict:
    return {
        "pair": label.pair,
        "generated_at": label.generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ttl_sec": label.ttl_sec,
        "regime": label.regime,
        "action": label.action,
        "size_multiplier": label.size_multiplier,
        "confidence": label.confidence,
        "event_flags": list(label.event_flags),
        "evidence": [e.model_dump() for e in label.evidence],
    }


def dump_decision(decision: GateDecision) -> str:
    return json.dumps(decision.to_audit(), ensure_ascii=False, default=str, sort_keys=True)
