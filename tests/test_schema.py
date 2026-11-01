"""标签 Schema 单元测试（G-12：严格 JSON + 重试 + 兜底）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aiquant.label_service.schema import (
    Label,
    SchemaError,
    fallback_allow_label,
    parse_label,
    parse_with_retry,
)
from helpers import label_dict, label_json, make_label


class TestValidLabel:
    def test_roundtrip(self):
        raw = label_json(pair="BTC/USDT", action="reduce_size", multiplier=0.4)
        label = parse_label(raw)
        assert label.pair == "BTC/USDT"
        assert label.action == "reduce_size"
        assert label.size_multiplier == 0.4
        assert parse_label(label.to_json()) == label

    def test_pair_normalized(self):
        data = label_dict()
        data["pair"] = " btc/usdt "
        assert parse_label(json.dumps(data)).pair == "BTC/USDT"

    def test_expiry_property(self):
        now = datetime.now(timezone.utc)
        label = make_label(ttl_sec=120, now=now)
        assert label.expiry() == now + timedelta(seconds=120)
        assert not label.is_expired(now + timedelta(seconds=60))
        assert label.is_expired(now + timedelta(seconds=121))


class TestStrictJson:
    def test_markdown_fence_rejected(self):
        with pytest.raises(SchemaError):
            parse_label("```json\n" + label_json() + "\n```")

    def test_surrounding_prose_rejected(self):
        with pytest.raises(SchemaError):
            parse_label("结果是：" + label_json())

    def test_non_object_rejected(self):
        with pytest.raises(SchemaError):
            parse_label("[1,2,3]")

    def test_invalid_json_rejected(self):
        with pytest.raises(SchemaError):
            parse_label("{not json}")

    def test_non_string_rejected(self):
        with pytest.raises(SchemaError):
            parse_label(123)  # type: ignore[arg-type]


class TestSchemaRules:
    def test_unknown_regime_rejected(self):
        data = label_dict()
        data["regime"] = "euphoria"
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_unknown_action_rejected(self):
        data = label_dict()
        data["action"] = "yolo_full_margin"
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_extra_field_rejected(self):
        data = label_dict()
        data["risk_score"] = 0.9
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_allow_requires_full_multiplier(self):
        data = label_dict()
        data["action"] = "allow"
        data["size_multiplier"] = 0.5
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_reduce_requires_positive_multiplier(self):
        data = label_dict()
        data["action"] = "reduce_size"
        data["size_multiplier"] = 0.0
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_reduce_multiplier_upper_bound(self):
        data = label_dict()
        data["action"] = "reduce_size"
        data["size_multiplier"] = 1.5
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_naive_datetime_rejected(self):
        data = label_dict()
        data["generated_at"] = "2026-01-01T00:00:00"
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_too_much_evidence_rejected(self):
        data = label_dict()
        data["evidence"] = [{"source": "okx", "summary": f"n{i}"} for i in range(11)]
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_unknown_event_flag_rejected(self):
        data = label_dict()
        data["event_flags"] = ["moon_phase"]
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_duplicate_flags_deduped(self):
        data = label_dict()
        data["event_flags"] = ["macro_event", "macro_event", "regulatory"]
        label = parse_label(json.dumps(data))
        assert label.event_flags == ["macro_event", "regulatory"]

    def test_evidence_credentials_rejected(self):
        data = label_dict()
        data["evidence"] = [{"source": "note", "summary": "api_key: sk-live-abcdef123456"}]
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))

    def test_pair_without_slash_rejected(self):
        data = label_dict()
        data["pair"] = "BTCUSDT"
        with pytest.raises(SchemaError):
            parse_label(json.dumps(data))


class TestFallback:
    def test_fallback_is_allow(self):
        label = fallback_allow_label("ETH/USDT", "llm timeout")
        assert label.action == "allow"
        assert label.size_multiplier == 1.0
        assert label.regime == "unknown"
        assert label.evidence[0].ref == "fallback"
        assert "llm timeout" in label.evidence[0].summary

    def test_fallback_reason_truncated(self):
        label = fallback_allow_label("ETH/USDT", "x" * 400)
        assert len(label.evidence[0].summary) <= 280


class TestParseWithRetry:
    def test_first_attempt_valid(self):
        calls = []

        def gen() -> str:
            calls.append(1)
            return label_json()

        label, attempts = parse_with_retry(gen, "BTC/USDT")
        assert attempts == 1
        assert len(calls) == 1
        assert label.action == "allow"

    def test_retry_then_valid(self):
        responses = iter(["not json at all", label_json()])
        retries: list[tuple[int, str]] = []

        label, attempts = parse_with_retry(
            lambda: next(responses), "BTC/USDT", retries=1, on_retry=lambda a, e: retries.append((a, e))
        )
        assert attempts == 2
        assert len(retries) == 1
        assert label.evidence == []

    def test_all_invalid_falls_back_to_allow(self):
        _, attempts = parse_with_retry(lambda: "{}", "BTC/USDT", retries=1)
        assert attempts == 2

        label, attempts = parse_with_retry(lambda: "{}", "BTC/USDT", retries=0)
        assert label.action == "allow"
        assert label.evidence[0].ref == "fallback"
        assert attempts == 1

    def test_pair_mismatch_falls_back(self):
        label, _ = parse_with_retry(lambda: label_json(pair="SOL/USDT"), "BTC/USDT", retries=0)
        assert label.pair == "BTC/USDT"
        assert label.evidence[0].ref == "fallback"
        assert "pair 不匹配" in label.evidence[0].summary

    def test_generator_exception_falls_back(self):
        def boom() -> str:
            raise RuntimeError("llm down")

        label, attempts = parse_with_retry(boom, "BTC/USDT", retries=1)
        assert attempts == 2
        assert label.action == "allow"
        assert "generator error" in label.evidence[0].summary


def test_label_model_dump_json_has_z_suffix():
    payload = json.loads(make_label().to_json())
    assert payload["generated_at"].endswith("Z")
    assert Label.model_validate(payload).pair == "BTC/USDT"
