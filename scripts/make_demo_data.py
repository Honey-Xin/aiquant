"""生成验证模块用的演示数据：``data/bars.csv`` 与 ``data/labels.jsonl``。

用途：
  * 本地 smoke test（``python -m aiquant.validation.cli ...``）；
  * 文档与 CI 里的可复现样例。
数据是合成行情（几何随机游走 + 周期性回撤），只用于验证管线，不构成任何投资建议。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_bars(n: int, start_price: float, seed: int) -> list[dict[str, object]]:
    rng = random.Random(seed)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    price = start_price
    rows: list[dict[str, object]] = []
    for i in range(n):
        drift = 0.0004 * math.sin(i / 40.0)
        shock = rng.gauss(0, 0.0025)
        price = max(1.0, price * (1 + drift + shock))
        rows.append(
            {
                "ts": (t0 + timedelta(minutes=5 * i)).isoformat().replace("+00:00", "Z"),
                "price": round(price, 4),
                "pair": "BTC/USDT",
            }
        )
    return rows


def make_labels(bars: list[dict[str, object]], every: int, ttl_sec: int, seed: int) -> list[dict]:
    rng = random.Random(seed + 1)
    records: list[dict] = []
    for i in range(0, len(bars), every):
        draw = rng.random()
        if draw < 0.55:
            action, multiplier, regime = "allow", 1.0, "risk_on"
        elif draw < 0.85:
            action, multiplier, regime = "reduce_size", round(rng.uniform(0.3, 0.9), 2), "event_driven"
        else:
            action, multiplier, regime = "block_new_entries", 1.0, "risk_off"
        label = {
            "pair": "BTC/USDT",
            "generated_at": bars[i]["ts"],
            "ttl_sec": ttl_sec,
            "regime": regime,
            "event_flags": [],
            "action": action,
            "size_multiplier": multiplier,
            "confidence": "medium",
            "evidence": [{"source": "demo", "summary": "合成数据", "ref": "demo"}],
        }
        records.append({"ts": bars[i]["ts"], "pair": "BTC/USDT", "label": label})
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="生成演示 bars/labels")
    parser.add_argument("--bars", type=int, default=3000)
    parser.add_argument("--out-dir", default=str(ROOT / "data"))
    parser.add_argument("--every", type=int, default=3, help="每 N 根 bar 生成一条标签")
    parser.add_argument("--ttl-sec", type=int, default=900)
    parser.add_argument("--seed", type=int, default=20260101)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bars = make_bars(args.bars, 20000.0, args.seed)
    bars_path = out_dir / "bars.csv"
    with bars_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["ts", "price", "pair"])
        writer.writeheader()
        writer.writerows(bars)

    labels = make_labels(bars, max(1, args.every), args.ttl_sec, args.seed)
    labels_path = out_dir / "labels.jsonl"
    with labels_path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in labels:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"bars   -> {bars_path} ({len(bars)} 行)")
    print(f"labels -> {labels_path} ({len(labels)} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
