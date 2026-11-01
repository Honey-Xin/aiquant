"""闸门裁决测试：覆盖 G-01 ~ G-05、G-10、G-11 的裁决侧。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aiquant import metrics as M
from aiquant.audit.logger import AuditLog
from aiquant.gate.decision import (
    HALT_KEY,
    RESULT_ALLOW,
    RESULT_BLOCK,
    RESULT_DISABLED,
    RESULT_ERROR,
    RESULT_MISSING,
    RESULT_REDUCE,
    Gate,
    GateDecision,
    GateToggle,
    dump_decision,
)
from helpers import FlakyStore, make_label, put_label

PAIR = "BTC/USDT"


def build_gate(store, tmp_path, *, fail_open: bool = True, min_amounts=None, default_enabled: bool = True):
    audit = AuditLog(tmp_path / "logs", 180, ())
    toggle = GateToggle(store=store, default=default_enabled, audit=audit)
    return Gate(
        store=store,
        toggle=toggle,
        audit=audit,
        min_amounts=min_amounts if min_amounts is not None else {PAIR: 0.001},
        fail_open=fail_open,
    )


class TestMissingAndExpired:
    def test_g01_missing_label_allows(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        decision = gate.evaluate(PAIR, 0.5)
        assert decision.allowed is True
        assert decision.result == RESULT_MISSING
        assert decision.adjusted_amount == 0.5
        assert decision.latency_ms >= 0.0

    def test_g02_expired_label_allows(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        old = datetime.now(timezone.utc) - timedelta(seconds=600)
        put_label(store, make_label(now=old, ttl_sec=300))
        decision = gate.evaluate(PAIR, 0.5)
        assert decision.allowed is True
        assert decision.result == RESULT_MISSING
        assert "expired" in decision.reason

    def test_fresh_label_not_expired(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        put_label(store, make_label(ttl_sec=300))
        assert gate.evaluate(PAIR, 0.5).result == RESULT_ALLOW


class TestBlock:
    def test_g03_block_denies(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        put_label(store, make_label(action="block_new_entries", regime="risk_off"))
        decision = gate.evaluate(PAIR, 0.5)
        assert decision.allowed is False
        assert decision.result == RESULT_BLOCK
        assert decision.adjusted_amount == 0.0
        assert "block_new_entries" in decision.reason

    def test_block_ignores_multiplier(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        put_label(store, make_label(action="block_new_entries", multiplier=0.9))
        assert gate.evaluate(PAIR, 0.5).allowed is False


class TestReduce:
    def test_g04_reduce_scales_amount(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        put_label(store, make_label(action="reduce_size", multiplier=0.5, regime="event_driven"))
        decision = gate.evaluate(PAIR, 1.0)
        assert decision.allowed is True
        assert decision.result == RESULT_REDUCE
        assert decision.size_multiplier == 0.5
        assert decision.adjusted_amount == pytest.approx(0.5)

    def test_g04_reduce_below_min_denies(self, store, tmp_path):
        gate = build_gate(store, tmp_path, min_amounts={PAIR: 1.0})
        put_label(store, make_label(action="reduce_size", multiplier=0.1))
        decision = gate.evaluate(PAIR, 2.0)
        assert decision.allowed is False
        assert decision.result == RESULT_REDUCE
        assert "最小下单量" in decision.reason

    def test_reduce_without_floor_allows(self, store, tmp_path):
        gate = build_gate(store, tmp_path, min_amounts={})
        put_label(store, make_label(action="reduce_size", multiplier=0.01))
        assert gate.evaluate(PAIR, 1.0).allowed is True

    def test_min_amount_lookup(self, store, tmp_path):
        gate = build_gate(store, tmp_path, min_amounts={PAIR: 0.25})
        assert gate.min_amount(PAIR) == 0.25
        assert gate.min_amount("SOL/USDT") == 0.0


class TestErrorPaths:
    def test_g05_invalid_json_fails_open(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        store.setex(f"ai_gate:{PAIR}", 300, "not-a-json")
        decision = gate.evaluate(PAIR, 0.5)
        assert decision.allowed is True
        assert decision.result == RESULT_ERROR

    def test_g05_schema_violation_fails_open(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        store.setex(f"ai_gate:{PAIR}", 300, '{"pair": "BTC/USDT"}')
        assert gate.evaluate(PAIR, 0.5).result == RESULT_ERROR

    def test_store_failure_fails_open(self, store, tmp_path):
        flaky = FlakyStore()
        flaky.fail_get = True
        gate = build_gate(flaky, tmp_path)
        decision = gate.evaluate(PAIR, 0.5)
        assert decision.allowed is True
        assert decision.result == RESULT_ERROR

    def test_fail_open_disabled_denies(self, store, tmp_path):
        flaky = FlakyStore()
        flaky.fail_get = True
        gate = build_gate(flaky, tmp_path, fail_open=False)
        decision = gate.evaluate(PAIR, 0.5)
        assert decision.allowed is False
        assert "fail-closed" in decision.reason

    def test_confirm_trade_entry_catches_internal_exception(self, store, tmp_path):
        gate = build_gate(store, tmp_path)

        def boom(*_args, **_kwargs):
            raise RuntimeError("unexpected")

        gate.read_label = boom  # type: ignore[assignment]
        allowed, decision = gate.confirm_trade_entry(PAIR, 0.5)
        assert allowed is True
        assert decision.result == RESULT_ERROR


class TestDisabled:
    def test_g10_disabled_gate_passes_through(self, store, tmp_path):
        gate = build_gate(store, tmp_path, default_enabled=False)
        put_label(store, make_label(action="block_new_entries"))
        decision = gate.evaluate(PAIR, 0.5)
        assert decision.allowed is True
        assert decision.result == RESULT_DISABLED

    def test_store_override_beats_default(self, store, tmp_path):
        gate = build_gate(store, tmp_path, default_enabled=True)
        store.setex("system:gate_enabled", 60, "0")
        put_label(store, make_label(action="block_new_entries"))
        assert gate.evaluate(PAIR, 0.5).result == RESULT_DISABLED

    def test_toggle_reads_env(self, store, tmp_path, monkeypatch):
        toggle = GateToggle(store=store, default=True)
        monkeypatch.setenv("GATE_ENABLED", "false")
        assert toggle.is_enabled() is False
        monkeypatch.setenv("GATE_ENABLED", "1")
        assert toggle.is_enabled() is True

    def test_toggle_audits_change(self, store, tmp_path):
        audit = AuditLog(tmp_path / "logs", 180, ())
        toggle = GateToggle(store=store, default=True, audit=audit)
        assert toggle.is_enabled() is True
        store.setex("system:gate_enabled", 60, "0")
        assert toggle.is_enabled() is False
        records = [r for r in audit.iter_records(stream="gate") if r["event"] == "gate_toggle_changed"]
        assert records and records[-1]["enabled"] is False

    def test_toggle_store_error_falls_back(self, tmp_path):
        flaky = FlakyStore()
        flaky.fail_get = True
        toggle = GateToggle(store=flaky, default=True)
        assert toggle.is_enabled() is True


class TestHelpers:
    def test_size_multiplier(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        put_label(store, make_label(action="reduce_size", multiplier=0.3))
        assert gate.size_multiplier(PAIR) == pytest.approx(0.3)
        put_label(store, make_label(action="block_new_entries"))
        assert gate.size_multiplier(PAIR) == 0.0
        put_label(store, make_label(action="allow"))
        assert gate.size_multiplier(PAIR) == 1.0

    def test_halt_active(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        assert gate.halt_active() is False
        store.setex(HALT_KEY, 60, "1")
        assert gate.halt_active() is True
        assert M.SYSTEM_HALT.value() == 1.0

    def test_halt_store_error_is_false(self, tmp_path):
        flaky = FlakyStore()
        flaky.fail_get = True
        gate = build_gate(flaky, tmp_path)
        assert gate.halt_active() is False

    def test_audit_written(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        put_label(store, make_label(action="block_new_entries"))
        gate.evaluate(PAIR, 0.5)
        records = [r for r in gate.audit.iter_records(stream="gate") if r["event"] == "gate_decision"]
        assert records
        assert records[-1]["result"] == RESULT_BLOCK
        assert records[-1]["pair"] == PAIR

    def test_metrics_recorded(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        before = M.GATE_TOTAL.value(result=RESULT_MISSING)
        gate.evaluate(PAIR, 0.5)
        assert M.GATE_TOTAL.value(result=RESULT_MISSING) == before + 1

    def test_dump_decision_json(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        decision = gate.evaluate(PAIR, 0.5)
        text = dump_decision(decision)
        assert '"result": "missing"' in text

    def test_decision_to_audit_shape(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        payload = gate.evaluate(PAIR, 0.5).to_audit()
        assert set(payload) >= {
            "pair",
            "result",
            "allowed",
            "reason",
            "original_amount",
            "adjusted_amount",
            "size_multiplier",
            "latency_ms",
        }

    def test_label_snapshot_in_decision(self, store, tmp_path):
        gate = build_gate(store, tmp_path)
        put_label(store, make_label(action="reduce_size", multiplier=0.5, flags=["macro_event"]))
        decision = gate.evaluate(PAIR, 1.0)
        assert decision.label is not None
        assert decision.label["event_flags"] == ["macro_event"]
        assert decision.label["size_multiplier"] == 0.5
