"""验证模块测试（G-08：禁未来函数）+ A/B 判定。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aiquant.validation import cli as vcli
from aiquant.validation.replay import (
    Bar,
    LabelHistory,
    LabelRecord,
    ReplayEngine,
    ReplayResult,
    ReplayStore,
    ReplayTrade,
    parse_ts,
)
from aiquant.validation.report import (
    Metrics,
    WindowComparison,
    compare,
    compute_metrics,
    decide,
    max_drawdown,
    sharpe_ratio,
)
from aiquant.validation.wfa import Fold, PurgedWalkForward, assert_no_overlap

from helpers import make_label

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
PAIR = "BTC/USDT"


def bars(n: int = 40, start: float = 100.0, step: float = 1.0):
    return [Bar(ts=T0 + timedelta(minutes=5 * i), price=start + step * i, pair=PAIR) for i in range(n)]


def history(*labels):
    return LabelHistory(LabelRecord(ts=lb.generated_at, pair=lb.pair, label=lb) for lb in labels)


class TestParseTs:
    def test_iso_string(self):
        assert parse_ts("2026-01-01T00:00:00Z") == T0

    def test_naive_string_becomes_utc(self):
        assert parse_ts("2026-01-01T00:00:00").tzinfo is not None

    def test_epoch(self):
        assert parse_ts(T0.timestamp()) == T0

    def test_datetime_passthrough(self):
        assert parse_ts(T0) == T0


class TestLabelHistory:
    def test_as_of_picks_latest_visible(self):
        first = make_label(now=T0, action="allow")
        second = make_label(now=T0 + timedelta(minutes=10), action="block_new_entries")
        hist = history(first, second)
        assert hist.as_of(T0)["BTC/USDT"].action == "allow"
        assert hist.as_of(T0 + timedelta(minutes=11))["BTC/USDT"].action == "block_new_entries"
        assert hist.as_of(T0 - timedelta(minutes=1)) == {}

    def test_g08_no_future_leak(self):
        future = make_label(now=T0 + timedelta(hours=1), action="block_new_entries")
        hist = history(future)
        assert hist.as_of(T0) == {}
        assert len(hist) == 1
        assert hist.pairs == [PAIR]

    def test_from_records_nested(self):
        label = make_label(now=T0)
        rows = [{"ts": T0.isoformat(), "pair": PAIR, "label": json.loads(label.to_json())}]
        hist = LabelHistory.from_records(rows)
        assert hist.as_of(T0)["BTC/USDT"].pair == PAIR

    def test_from_records_skips_broken(self):
        rows = [{"pair": PAIR}, {"ts": T0.isoformat()}]
        assert len(LabelHistory.from_records(rows)) == 0

    def test_from_jsonl_skips_bad_lines(self, tmp_path):
        label = make_label(now=T0)
        path = tmp_path / "labels.jsonl"
        path.write_text(
            "\n".join(
                [
                    "not-json",
                    json.dumps({"ts": T0.isoformat(), "pair": PAIR, "label": json.loads(label.to_json())}),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        assert len(LabelHistory.from_jsonl(path)) == 1


class TestReplayStore:
    def test_get_decodes_label(self):
        store = ReplayStore({PAIR: make_label(now=T0)})
        assert store.get("ai_gate:BTC/USDT") is not None
        assert store.get("other:key") is None

    def test_advance_replaces_snapshot(self):
        store = ReplayStore({PAIR: make_label(now=T0)})
        store.advance({})
        assert store.get("ai_gate:BTC/USDT") is None

    def test_ping(self):
        assert ReplayStore().ping() is True


class TestReplayEngine:
    def test_allow_label_trades(self):
        result = ReplayEngine({PAIR: bars()}, history(make_label(now=T0, action="allow")), hold_bars=3).run()
        assert result.trades
        assert result.gate_counts.get("allow", 0) > 0
        assert result.trades[0].pnl > 0  # 价格线性上涨
        assert result.equity_curve

    def test_block_label_prevents_trades(self):
        blocked = make_label(now=T0, action="block_new_entries", regime="risk_off", ttl_sec=86_400)
        result = ReplayEngine({PAIR: bars()}, history(blocked), hold_bars=3).run()
        assert result.trades == []
        assert result.gate_counts.get("block", 0) > 0
        assert result.blocked > 0

    def test_reduce_label_scales_position(self):
        reduced = make_label(now=T0, action="reduce_size", multiplier=0.5)
        result = ReplayEngine({PAIR: bars()}, history(reduced), hold_bars=3, stake=100.0).run()
        assert result.trades[0].size_multiplier == 0.5
        assert result.gate_counts.get("reduce", 0) > 0

    def test_missing_label_trades_full_size(self):
        result = ReplayEngine({PAIR: bars(20)}, LabelHistory([]), hold_bars=2).run()
        assert result.gate_counts.get("missing", 0) > 0
        assert result.trades

    def test_gate_disabled_uses_disabled_result(self):
        blocked = make_label(now=T0, action="block_new_entries")
        result = ReplayEngine({PAIR: bars()}, history(blocked), hold_bars=3).run(gate_enabled=False)
        assert result.trades
        assert result.gate_counts.get("disabled", 0) > 0

    def test_label_expiry_mid_run(self):
        short = make_label(now=T0, action="block_new_entries", ttl_sec=300)
        result = ReplayEngine({PAIR: bars(n=10)}, history(short), hold_bars=2).run()
        # 前 300s 内被拦，之后标签过期 → missing 放行
        assert result.gate_counts.get("block", 0) >= 1
        assert result.gate_counts.get("missing", 0) >= 1

    def test_empty_bars(self):
        result = ReplayEngine({}, LabelHistory([])).run()
        assert result.trades == []
        assert result.equity_curve

    def test_capital_exhausted(self):
        result = ReplayEngine({PAIR: bars(n=6)}, LabelHistory([]), stake=2000.0, initial_capital=1000.0).run()
        assert result.trades == []

    def test_trade_pnl_properties(self):
        trade = ReplayTrade(
            pair=PAIR,
            entry_ts=T0,
            exit_ts=T0 + timedelta(minutes=5),
            entry_price=100.0,
            exit_price=110.0,
            stake=100.0,
            gate_result="allow",
        )
        assert trade.pnl == pytest.approx(10.0)
        assert trade.pnl_pct == pytest.approx(0.1)


class TestWalkForward:
    def test_splits_cover_samples(self):
        wfa = PurgedWalkForward(n_splits=4, embargo=10)
        folds = wfa.split_list(500)
        assert len(folds) == 4
        assert_no_overlap(folds, 500)
        assert all(f.train_end <= f.test_start for f in folds)
        assert folds[0].test_start == folds[0].train_end + 10

    def test_embargo_gap_respected(self):
        folds = PurgedWalkForward(n_splits=3, embargo=24).split_list(300)
        for fold in folds:
            assert fold.test_start - fold.train_end == 24

    def test_expanding_train_grows(self):
        folds = PurgedWalkForward(n_splits=3, embargo=5).split_list(400)
        assert folds[0].train_start == 0
        assert folds[-1].train_end > folds[0].train_end

    def test_rolling_window(self):
        folds = PurgedWalkForward(n_splits=3, embargo=5, expanding=False).split_list(400)
        assert all(f.train_size <= folds[0].train_size for f in folds)

    def test_invalid_params(self):
        with pytest.raises(ValueError):
            PurgedWalkForward(n_splits=0)
        with pytest.raises(ValueError):
            PurgedWalkForward(embargo=-1)

    def test_small_sample_yields_nothing(self):
        assert PurgedWalkForward(n_splits=4, embargo=1).split_list(3) == []
        assert PurgedWalkForward().split_list(0) == []

    def test_min_train_size(self):
        folds = PurgedWalkForward(n_splits=2, embargo=1, min_train_size=50).split_list(200)
        assert folds[0].train_size >= 49

    def test_fold_properties(self):
        fold = Fold(index=0, train_start=0, train_end=10, test_start=12, test_end=20, embargo=2)
        assert fold.train_size == 10 and fold.test_size == 8

    def test_assert_no_overlap_detects_overlap(self):
        bad = [
            Fold(index=0, train_start=0, train_end=5, test_start=5, test_end=10, embargo=0),
            Fold(index=1, train_start=0, train_end=9, test_start=9, test_end=12, embargo=0),
        ]
        with pytest.raises(AssertionError):
            assert_no_overlap(bad, 20)


class TestMetrics:
    def test_sharpe_and_mdd(self):
        assert sharpe_ratio([0.01], 100) == 0.0
        assert sharpe_ratio([0.0, 0.0, 0.0], 100) == 0.0
        assert sharpe_ratio([0.01, 0.02, 0.03, 0.01], 1000) > 0
        assert max_drawdown([100, 80, 120, 60]) == pytest.approx(0.5)
        assert max_drawdown([100, 200]) == 0.0

    def test_compute_metrics(self):
        result = ReplayResult(
            equity_curve=[(T0, 1000.0), (T0, 1010.0), (T0, 1005.0)],
            trades=[
                ReplayTrade(PAIR, T0, T0, 100.0, 110.0, 100.0, "allow"),
                ReplayTrade(PAIR, T0, T0, 100.0, 95.0, 100.0, "allow"),
            ],
            gate_counts={"allow": 2},
            initial_capital=1000.0,
        )
        metrics = compute_metrics(result, periods_per_year=1000)
        assert metrics.trades == 2
        assert metrics.win_rate == 0.5
        assert metrics.profit_factor == pytest.approx(2.0)
        assert metrics.total_return == pytest.approx(0.005)
        assert metrics.to_dict()["gate_counts"] == {"allow": 2}

    def test_empty_result(self):
        metrics = compute_metrics(ReplayResult(initial_capital=1000.0), 1000)
        assert metrics.trades == 0 and metrics.sharpe == 0.0

    def test_profit_factor_infinite_without_losses(self):
        result = ReplayResult(
            equity_curve=[(T0, 1000.0), (T0, 1010.0)],
            trades=[ReplayTrade(PAIR, T0, T0, 100.0, 110.0, 100.0, "allow")],
        )
        assert compute_metrics(result, 1000).profit_factor == float("inf")


class TestDecide:
    def base(self, **kwargs):
        defaults = dict(sharpe=0.5, max_drawdown=0.2, trades=10)
        defaults.update(kwargs)
        return Metrics(**defaults)

    def test_approve_on_sharpe(self):
        approved, reasons = decide(self.base(), self.base(sharpe=0.75))
        assert approved is True
        assert any("PASS" in r for r in reasons)

    def test_approve_on_mdd(self):
        approved, _ = decide(self.base(), self.base(max_drawdown=0.15))
        assert approved is True

    def test_reject_when_trades_drop_too_much(self):
        approved, reasons = decide(self.base(), self.base(sharpe=0.9, trades=5))
        assert approved is False
        assert any("交易数变化" in r and "FAIL" in r for r in reasons)

    def test_reject_on_bad_window(self):
        window = WindowComparison(
            index=0, baseline_mdd=0.01, treatment_mdd=0.05, baseline_return=-0.01, treatment_return=-0.10
        )
        approved, _ = decide(self.base(), self.base(sharpe=0.9), [window])
        assert approved is False

    def test_window_ok_when_beats_baseline(self):
        window = WindowComparison(
            index=0, baseline_mdd=0.01, treatment_mdd=0.02, baseline_return=-0.05, treatment_return=-0.02
        )
        approved, _ = decide(self.base(), self.base(sharpe=0.9), [window])
        assert approved is True

    def test_zero_baseline_mdd(self):
        approved, _ = decide(Metrics(sharpe=0.0, max_drawdown=0.0, trades=0), Metrics(sharpe=0.5))
        assert approved is True


class TestCompare:
    def test_report_shape(self):
        engine = ReplayEngine({PAIR: bars()}, history(make_label(now=T0)), hold_bars=3)
        baseline = engine.run(gate_enabled=False)
        treatment = ReplayEngine(
            {PAIR: bars()}, history(make_label(now=T0, action="reduce_size", multiplier=0.5)), hold_bars=3
        ).run()
        report = compare(baseline, treatment, periods_per_year=1000)
        payload = json.loads(report.to_json())
        assert "baseline" in payload and "treatment" in payload
        assert payload["approved"] in {True, False}
        assert isinstance(payload["reasons"], list)
        assert isinstance(report.windows, list)


class TestCli:
    def write_inputs(self, tmp_path, bars_n: int = 40):
        bars_path = tmp_path / "bars.csv"
        lines = ["pair,ts,price"]
        for bar in bars(bars_n):
            lines.append(f"{bar.pair},{bar.ts.isoformat()},{bar.price}")
        bars_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        label = make_label(now=T0, action="allow")
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text(
            json.dumps({"ts": T0.isoformat(), "pair": PAIR, "label": json.loads(label.to_json())}) + "\n",
            encoding="utf-8",
        )
        return bars_path, labels_path

    def test_load_bars_csv(self, tmp_path):
        bars_path, _ = self.write_inputs(tmp_path, 5)
        loaded = vcli.load_bars_csv(bars_path)
        assert len(loaded[PAIR]) == 5
        assert loaded[PAIR][0].price == 100.0

    def test_cli_writes_report(self, tmp_path):
        bars_path, labels_path = self.write_inputs(tmp_path)
        out = tmp_path / "reports" / "ab.json"
        code = vcli.main(["--labels", str(labels_path), "--bars", str(bars_path), "--out", str(out)])
        assert code in {0, 2}
        assert out.exists()
        json.loads(out.read_text(encoding="utf-8"))

    def test_cli_no_trades_exit_code_3(self, tmp_path):
        bars_path = tmp_path / "bars.csv"
        bars_path.write_text(f"pair,ts,price\n{PAIR},{T0.isoformat()},100\n", encoding="utf-8")
        labels_path = tmp_path / "labels.jsonl"
        labels_path.write_text("", encoding="utf-8")
        code = vcli.main(["--labels", str(labels_path), "--bars", str(bars_path), "--out", str(tmp_path / "r.json")])
        assert code == 3
