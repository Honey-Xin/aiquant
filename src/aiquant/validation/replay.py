"""回放引擎（FR-3.2）。

在同一份确定性 ``Gate`` 代码上回放历史标签，**严禁未来函数**：

* ``LabelHistory.as_of(ts)`` 只返回 ``generated_at <= ts`` 的标签；
* 回放时间轴上每根 bar 都用"截至该 bar"的标签快照重建存储，再走真实裁决路径；
* 不使用任何在 bar 之后才产生的数据（价格用 bar 收盘价成交）。

这样验证的结论可以反推到线上，而不是另写一套"验证版闸门"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from aiquant.audit.logger import AuditLog
from aiquant.gate.decision import Gate, GateDecision
from aiquant.label_service.schema import Label
from aiquant.label_service.store.base import StoreError

PAIR_SEP = "/"


def parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Bar:
    ts: datetime
    price: float
    pair: str = ""


@dataclass(frozen=True)
class LabelRecord:
    ts: datetime
    pair: str
    label: Label


class LabelHistory:
    def __init__(self, records: Iterable[LabelRecord]) -> None:
        self._by_pair: dict[str, list[LabelRecord]] = {}
        for rec in records:
            self._by_pair.setdefault(rec.pair, []).append(rec)
        for items in self._by_pair.values():
            items.sort(key=lambda r: r.ts)

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_pair.values())

    @property
    def pairs(self) -> list[str]:
        return sorted(self._by_pair)

    def as_of(self, ts: datetime) -> dict[str, Label]:
        """截至 ts 的"最新已发布标签"快照。"""
        ts = parse_ts(ts)
        out: dict[str, Label] = {}
        for pair, items in self._by_pair.items():
            chosen: Label | None = None
            for rec in items:
                if rec.ts <= ts:
                    chosen = rec.label
                else:
                    break
            if chosen is not None:
                out[pair] = chosen
        return out

    # ------------------------------------------------------------- 加载
    @classmethod
    def from_records(cls, rows: Iterable[Mapping[str, Any]]) -> "LabelHistory":
        records: list[LabelRecord] = []
        for row in rows:
            ts_raw = row.get("ts") or row.get("generated_at") or row.get("timestamp")
            label_raw = row.get("label")
            pair = row.get("pair")
            if label_raw is None:
                label_raw = row
                pair = row.get("pair")
            if ts_raw is None or pair is None or label_raw is None:
                continue
            label = Label.model_validate(label_raw) if not isinstance(label_raw, Label) else label_raw
            records.append(LabelRecord(ts=parse_ts(ts_raw), pair=str(pair), label=label))
        return cls(records)

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "LabelHistory":
        rows: list[dict[str, Any]] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return cls.from_records(rows)


class ReplayStore:
    """把"截至当前 bar 的标签快照"包成 ``LabelStore``，喂给真实的 Gate。"""

    def __init__(self, snapshot: Mapping[str, Label] | None = None) -> None:
        self.snapshot: dict[str, Label] = dict(snapshot or {})
        self.calls = 0

    def advance(self, snapshot: Mapping[str, Label]) -> None:
        self.snapshot = dict(snapshot)

    def get(self, key: str) -> str | None:
        if key.startswith("ai_gate:"):
            self.calls += 1
            pair = key.split("ai_gate:", 1)[1]
            label = self.snapshot.get(pair)
            return label.to_json() if label is not None else None
        return None

    def setex(self, key: str, ttl_sec: int, value: str) -> None:  # pragma: no cover - 回放只读
        return None

    def delete(self, key: str) -> None:  # pragma: no cover
        return None

    def ping(self) -> bool:
        return True

    def set_available(self, available: bool) -> None:  # pragma: no cover
        if not available:
            raise StoreError("replay store forced unavailable")


@dataclass
class ReplayTrade:
    pair: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    stake: float
    gate_result: str
    size_multiplier: float = 1.0

    @property
    def pnl(self) -> float:
        qty = self.stake / self.entry_price if self.entry_price else 0.0
        return qty * (self.exit_price - self.entry_price)

    @property
    def pnl_pct(self) -> float:
        return self.pnl / self.stake if self.stake else 0.0


@dataclass
class ReplayResult:
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    trades: list[ReplayTrade] = field(default_factory=list)
    gate_counts: dict[str, int] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    initial_capital: float = 0.0

    @property
    def blocked(self) -> int:
        return self.gate_counts.get("block", 0) + self.gate_counts.get("reduce", 0)


class ReplayEngine:
    def __init__(
        self,
        bars: Mapping[str, Iterable[Bar]],
        history: LabelHistory,
        stake: float = 100.0,
        hold_bars: int = 12,
        initial_capital: float = 1000.0,
        audit: AuditLog | None = None,
    ) -> None:
        self.bars = {pair: sorted(list(items), key=lambda b: b.ts) for pair, items in bars.items()}
        self.history = history
        self.stake = float(stake)
        self.hold_bars = int(hold_bars)
        self.initial_capital = float(initial_capital)
        self.audit = audit or AuditLog()

    def _timeline(self) -> list[tuple[datetime, str, int]]:
        points: list[tuple[datetime, str, int]] = []
        for pair, items in self.bars.items():
            for idx, bar in enumerate(items):
                points.append((bar.ts, pair, idx))
        points.sort(key=lambda p: (p[0], p[1]))
        return points

    def run(self, gate_enabled: bool = True) -> ReplayResult:
        store = ReplayStore()
        gate = Gate(store=store, audit=self.audit, fail_open=True)
        result = ReplayResult(initial_capital=self.initial_capital)
        cash = self.initial_capital
        price_of: dict[str, float] = {}
        open_pos: dict[str, tuple[datetime, float, int, GateDecision]] = {}
        result.equity_curve.append((self._timeline()[0][0], cash) if self._timeline() else (datetime.now(timezone.utc), cash))

        for ts, pair, idx in self._timeline():
            bar = self.bars[pair][idx]
            price_of[pair] = bar.price
            store.advance(self.history.as_of(ts))

            # 1) 到期平仓（按 bar 收盘价）
            if pair in open_pos:
                entry_ts, entry_price, entry_idx, decision = open_pos[pair]
                if idx - entry_idx >= self.hold_bars or idx == len(self.bars[pair]) - 1:
                    qty = (self.stake * decision.size_multiplier) / entry_price if entry_price else 0.0
                    cash += qty * bar.price
                    trade = ReplayTrade(
                        pair=pair,
                        entry_ts=entry_ts,
                        exit_ts=ts,
                        entry_price=entry_price,
                        exit_price=bar.price,
                        stake=self.stake,
                        gate_result=decision.result,
                        size_multiplier=decision.size_multiplier,
                    )
                    result.trades.append(trade)
                    del open_pos[pair]

            # 2) 空仓则问闸门；gate 关闭时等价于放行
            if pair in open_pos or cash < self.stake:
                result.equity_curve.append((ts, cash))
                continue

            decision = gate.evaluate(pair, self.stake, now=ts) if gate_enabled else GateDecision(
                allowed=True,
                result="disabled",
                reason="gate disabled in replay",
                pair=pair,
                original_amount=self.stake,
                adjusted_amount=self.stake,
            )
            result.gate_counts[decision.result] = result.gate_counts.get(decision.result, 0) + 1
            result.decisions.append({"ts": ts.isoformat(), **decision.to_audit()})

            if decision.allowed:
                cash -= self.stake * decision.size_multiplier
                open_pos[pair] = (ts, bar.price, idx, decision)
            result.equity_curve.append((ts, cash))

        return result
