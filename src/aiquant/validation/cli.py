"""验证 CLI：历史标签回放 + A/B 报告。

用法::

    python -m aiquant.validation.cli \
        --labels data/labels.jsonl \
        --bars data/bars.csv \
        --out reports/ab_report.json \
        --periods-per-year 105120

退出码：0 = approve，2 = reject，3 = 数据不足。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

from .replay import Bar, LabelHistory, ReplayEngine, ReplayResult, parse_ts
from .report import ABReport, compare


def load_bars_csv(path: str | Path) -> dict[str, list[Bar]]:
    bars: dict[str, list[Bar]] = {}
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            pair = (row.get("pair") or "").strip()
            ts_raw = (row.get("ts") or row.get("timestamp") or "").strip()
            price_raw = (row.get("price") or row.get("close") or "").strip()
            if not pair or not ts_raw or not price_raw:
                continue
            bars.setdefault(pair, []).append(Bar(ts=parse_ts(ts_raw), price=float(price_raw), pair=pair))
    for items in bars.values():
        items.sort(key=lambda b: b.ts)
    return bars


def run_ab(
    labels_path: str | Path,
    bars_path: str | Path,
    periods_per_year: float = 105_120.0,
    stake: float = 100.0,
    hold_bars: int = 12,
    initial_capital: float = 1000.0,
) -> tuple[ReplayResult, ReplayResult, ABReport]:
    history = LabelHistory.from_jsonl(labels_path)
    bars = load_bars_csv(bars_path)
    engine = ReplayEngine(bars=bars, history=history, stake=stake, hold_bars=hold_bars, initial_capital=initial_capital)
    baseline = engine.run(gate_enabled=False)
    treatment = engine.run(gate_enabled=True)
    report = compare(baseline, treatment, periods_per_year=periods_per_year)
    return baseline, treatment, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AI 量化守门员：A/B 验证")
    p.add_argument("--labels", required=True, help="历史标签 JSONL")
    p.add_argument("--bars", required=True, help="价格 CSV（pair,ts,price）")
    p.add_argument("--out", default="reports/ab_report.json", help="报告输出路径")
    p.add_argument("--periods-per-year", type=float, default=105_120.0, help="5m bar 的年化周期数")
    p.add_argument("--stake", type=float, default=100.0)
    p.add_argument("--hold-bars", type=int, default=12)
    p.add_argument("--initial-capital", type=float, default=1000.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    baseline, treatment, report = run_ab(
        args.labels,
        args.bars,
        periods_per_year=args.periods_per_year,
        stake=args.stake,
        hold_bars=args.hold_bars,
        initial_capital=args.initial_capital,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json(), encoding="utf-8")

    print(report.to_json())
    if not baseline.trades and not treatment.trades:
        print("数据不足：没有任何交易被触发", file=sys.stderr)
        return 3
    return 0 if report.approved else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["load_bars_csv", "run_ab", "main"]
