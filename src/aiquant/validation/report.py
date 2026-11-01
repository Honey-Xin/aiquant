"""A/B 报告与 approve 判定（FR-3.3）。

基线 A = 闸门关闭；实验 B = 闸门开启。所有指标都从回放结果算出，不引入外部假设。
判定规则（需求 §3.3，三条全部满足才 approve）：

1. 夏普提升 ≥ 0.1 **或** 最大回撤降低 ≥ 10%；
2. 交易数下降 ≤ 30%；
3. 没有任何单一 walk-forward 窗口的亏损超过基线该窗口 MDD 的 1.5 倍。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .replay import ReplayResult, ReplayTrade

SHARPE_MIN_GAIN = 0.1
MDD_MIN_IMPROVEMENT = 0.10
TRADES_MAX_DROP = 0.30
WORST_WINDOW_LOSS_FACTOR = 1.5


@dataclass
class Metrics:
    initial_capital: float = 0.0
    final_equity: float = 0.0
    total_return: float = 0.0
    annualized_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    turnover: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_hold_bars: float = 0.0
    gate_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_capital": round(self.initial_capital, 4),
            "final_equity": round(self.final_equity, 4),
            "total_return": round(self.total_return, 6),
            "annualized_return": round(self.annualized_return, 6),
            "sharpe": round(self.sharpe, 4),
            "max_drawdown": round(self.max_drawdown, 6),
            "turnover": round(self.turnover, 4),
            "trades": self.trades,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "avg_hold_bars": round(self.avg_hold_bars, 2),
            "gate_counts": dict(self.gate_counts),
        }


def _returns(equity: Iterable[float]) -> list[float]:
    series = list(equity)
    out: list[float] = []
    for prev, cur in zip(series, series[1:]):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown(equity: Iterable[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe_ratio(returns: Sequence[float], periods_per_year: float) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (mean / sd) * math.sqrt(periods_per_year)


def compute_metrics(
    result: ReplayResult,
    periods_per_year: float = 105_120.0,
    avg_hold_bars: float | None = None,
) -> Metrics:
    equity = [v for _, v in result.equity_curve]
    if not equity:
        return Metrics(gate_counts=dict(result.gate_counts))
    initial = result.initial_capital or equity[0]
    final = equity[-1]
    rets = _returns(equity)
    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    years = max(len(rets) / periods_per_year, 1e-9)
    total_return = final / initial - 1.0 if initial else 0.0
    annualized = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 and total_return > -1 else 0.0
    notional = sum(t.stake for t in result.trades) or 1.0

    return Metrics(
        initial_capital=initial,
        final_equity=final,
        total_return=total_return,
        annualized_return=annualized,
        sharpe=sharpe_ratio(rets, periods_per_year),
        max_drawdown=max_drawdown(equity),
        turnover=sum(t.stake for t in result.trades) / notional,
        trades=len(result.trades),
        win_rate=(len(wins) / len(result.trades)) if result.trades else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        avg_hold_bars=avg_hold_bars or 0.0,
        gate_counts=dict(result.gate_counts),
    )


@dataclass
class WindowComparison:
    index: int
    baseline_mdd: float
    treatment_mdd: float
    baseline_return: float
    treatment_return: float

    @property
    def ok(self) -> bool:
        limit = self.baseline_mdd * WORST_WINDOW_LOSS_FACTOR
        loss = max(0.0, -self.treatment_return)
        return loss <= max(limit, 0.0) or self.treatment_return >= self.baseline_return


def _window_metrics(result: ReplayResult, start: int, end: int, periods_per_year: float) -> tuple[float, float]:
    curve = [v for _, v in result.equity_curve][start:end]
    if len(curve) < 2:
        return 0.0, 0.0
    mdd = max_drawdown(curve)
    ret = curve[-1] / curve[0] - 1.0 if curve[0] else 0.0
    return mdd, ret


@dataclass
class ABReport:
    baseline: Metrics
    treatment: Metrics
    windows: list[WindowComparison] = field(default_factory=list)
    approved: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "treatment": self.treatment.to_dict(),
            "windows": [
                {
                    "index": w.index,
                    "baseline_mdd": round(w.baseline_mdd, 6),
                    "treatment_mdd": round(w.treatment_mdd, 6),
                    "baseline_return": round(w.baseline_return, 6),
                    "treatment_return": round(w.treatment_return, 6),
                    "ok": w.ok,
                }
                for w in self.windows
            ],
            "approved": self.approved,
            "reasons": self.reasons,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def compare(
    baseline: ReplayResult,
    treatment: ReplayResult,
    periods_per_year: float = 105_120.0,
    window_sizes: Sequence[int] | None = None,
) -> ABReport:
    base_metrics = compute_metrics(baseline, periods_per_year)
    treat_metrics = compute_metrics(treatment, periods_per_year)

    n = min(len(baseline.equity_curve), len(treatment.equity_curve))
    sizes = list(window_sizes or [])
    if not sizes and n > 0:
        fold_size = max(1, n // 4)
        sizes = [fold_size] * (n // fold_size)

    windows: list[WindowComparison] = []
    cursor = 0
    for idx, size in enumerate(sizes):
        start, end = cursor, min(cursor + size, n)
        if end - start < 2:
            break
        b_mdd, b_ret = _window_metrics(baseline, start, end, periods_per_year)
        t_mdd, t_ret = _window_metrics(treatment, start, end, periods_per_year)
        windows.append(
            WindowComparison(
                index=idx,
                baseline_mdd=b_mdd,
                treatment_mdd=t_mdd,
                baseline_return=b_ret,
                treatment_return=t_ret,
            )
        )
        cursor = end

    report = ABReport(baseline=base_metrics, treatment=treat_metrics, windows=windows)
    report.approved, report.reasons = decide(base_metrics, treat_metrics, windows)
    return report


def decide(
    baseline: Metrics,
    treatment: Metrics,
    windows: Sequence[WindowComparison] = (),
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    checks: list[bool] = []

    sharpe_gain = treatment.sharpe - baseline.sharpe
    mdd_improve = (baseline.max_drawdown - treatment.max_drawdown) / baseline.max_drawdown if baseline.max_drawdown > 0 else 0.0
    check1 = sharpe_gain >= SHARPE_MIN_GAIN or mdd_improve >= MDD_MIN_IMPROVEMENT
    checks.append(check1)
    reasons.append(
        f"[{'PASS' if check1 else 'FAIL'}] 夏普提升 {sharpe_gain:+.4f}（需 ≥ {SHARPE_MIN_GAIN}）"
        f" 或 MDD 改善 {mdd_improve:.2%}（需 ≥ {MDD_MIN_IMPROVEMENT:.0%}）"
    )

    if baseline.trades > 0:
        trade_drop = (baseline.trades - treatment.trades) / baseline.trades
    else:
        trade_drop = 0.0 if treatment.trades == 0 else 1.0
    check2 = trade_drop <= TRADES_MAX_DROP
    checks.append(check2)
    reasons.append(
        f"[{'PASS' if check2 else 'FAIL'}] 交易数变化 {trade_drop:+.2%}（下降不得超过 {TRADES_MAX_DROP:.0%}）"
    )

    bad_windows = [w for w in windows if not w.ok]
    check3 = not bad_windows
    checks.append(check3)
    if windows:
        reasons.append(
            f"[{'PASS' if check3 else 'FAIL'}] 窗口风险：{len(bad_windows)}/{len(windows)} 个窗口亏损超过基线 MDD 的 "
            f"{WORST_WINDOW_LOSS_FACTOR}×"
        )
    else:
        reasons.append("[PASS] 无窗口数据，跳过窗口风险检查")

    approved = all(checks)
    if approved:
        reasons.append("结论：approve —— 三条规则全部满足")
    else:
        reasons.append("结论：reject —— 未满足全部 approve 条件")
    return approved, reasons


__all__ = [
    "Metrics",
    "ABReport",
    "WindowComparison",
    "compute_metrics",
    "compare",
    "decide",
    "max_drawdown",
    "sharpe_ratio",
    "SHARPE_MIN_GAIN",
    "MDD_MIN_IMPROVEMENT",
    "TRADES_MAX_DROP",
    "WORST_WINDOW_LOSS_FACTOR",
]
