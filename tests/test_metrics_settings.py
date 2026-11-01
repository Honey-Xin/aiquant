"""配置与指标实现测试（G-11：secrets 收集；GATE_FAIL_OPEN 强制 true）。"""

from __future__ import annotations

import pytest

from aiquant import metrics as M
from aiquant.settings import Settings, get_settings, reset_settings_cache


class TestSettings:
    def test_gate_fail_open_must_be_true(self, settings_factory):
        with pytest.raises(ValueError, match="GATE_FAIL_OPEN"):
            settings_factory(gate_fail_open=False)

    def test_pairs_parsing(self, settings_factory):
        s = settings_factory(label_pairs=" BTC/USDT , eth/usdt ,, ")
        assert s.pairs == ["BTC/USDT", "ETH/USDT"]

    def test_min_amounts_parsing(self, settings_factory):
        s = settings_factory(gate_min_amount_json='{"BTC/USDT": 0.001}')
        assert s.min_amounts == {"BTC/USDT": 0.001}

    def test_min_amounts_bad_json_is_empty(self, settings_factory):
        assert settings_factory(gate_min_amount_json="not-json").min_amounts == {}

    def test_min_amounts_non_object_is_empty(self, settings_factory):
        assert settings_factory(gate_min_amount_json="[1,2]").min_amounts == {}

    def test_secrets_collected(self, settings_factory):
        s = settings_factory(
            llm_api_key="llm-secret-key",
            okx_api_secret="okx-secret-value",
            okx_api_passphrase="passphrase-value",
            gate_api_token="gate-token-value",
            freqtrade_api_password="ft-password",
        )
        secrets = s.secrets()
        assert "llm-secret-key" in secrets
        assert "okx-secret-value" in secrets
        assert "gate-token-value" in secrets

    def test_short_secrets_skipped(self, settings_factory):
        s = settings_factory(llm_api_key="ab")
        assert "ab" not in s.secrets()

    def test_get_settings_cached(self, monkeypatch):
        reset_settings_cache()
        first = get_settings()
        assert get_settings() is first
        reset_settings_cache()
        assert get_settings() is not first


class TestMetricsPrimitives:
    def test_counter_labels(self):
        counter = M.Counter("test_counter", "help", ("a",))
        counter.inc(a="x")
        counter.inc(2, a="x")
        counter.inc(a="y")
        assert counter.value(a="x") == 3
        assert counter.value(a="y") == 1
        text = "\n".join(counter.render())
        assert 'test_counter{a="x"} 3' in text
        assert "# TYPE test_counter counter" in text

    def test_gauge(self):
        gauge = M.Gauge("test_gauge", "help")
        gauge.set(1.5)
        assert gauge.value() == 1.5
        assert "test_gauge 1.5" in "\n".join(gauge.render())

    def test_histogram(self):
        hist = M.Histogram("test_hist", "help", ("action",), buckets=(0.1, 1.0))
        hist.observe(0.05, action="buy")
        hist.observe(0.5, action="buy")
        hist.observe(5.0, action="buy")
        text = "\n".join(hist.render())
        assert 'test_hist_bucket{action="buy",le="0.1"} 1' in text
        assert 'test_hist_bucket{action="buy",le="+Inf"} 3' in text
        assert 'test_hist_count{action="buy"} 3' in text
        assert 'test_hist_sum{action="buy"}' in text

    def test_label_escaping(self):
        counter = M.Counter("test_escape", "help", ("pair",))
        counter.inc(pair='a"b\nc')
        assert '\\"b\\nc' in "\n".join(counter.render())

    def test_registry_render_and_reset(self):
        registry = M.Registry()
        metric = registry.register(M.Counter("test_registry", "help"))
        metric.inc()
        assert "test_registry 1" in registry.render()
        registry.reset()
        assert "test_registry 0" in registry.render() or "test_registry" not in registry.render()

    def test_global_render_contains_core_metrics(self):
        text = M.render_metrics()
        for name in (
            "aiquant_label_generation_total",
            "aiquant_gate_total",
            "aiquant_gate_enabled",
            "aiquant_cli_calls",
            "aiquant_cli_seconds",
            "aiquant_freqtrade_paused",
            "aiquant_system_halt",
        ):
            assert name in text

    def test_inf_formatting(self):
        assert M._fmt(float("inf")) == "+Inf"
        assert M._fmt(2.0) == "2"
        assert M._fmt(0.5) == "0.5"
