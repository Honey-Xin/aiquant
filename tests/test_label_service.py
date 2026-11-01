"""标签服务测试（G-12：Schema 重试；FR-1.5 心跳）。"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aiquant.audit.logger import AuditLog
from aiquant.label_service.llm.client import LLMError, StaticLLMClient
from aiquant.label_service.main import LabelService, create_app
from aiquant.label_service.readonly_check import ReadonlyCheckResult
from aiquant.label_service.sources.okx_mcp import MarketSnapshot
from aiquant.label_service.store.base import StoreError

from helpers import FlakyStore, label_json

PAIR = "BTC/USDT"


class FakeMcp:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    def snapshot(self, pair: str) -> MarketSnapshot:
        self.calls.append(pair)
        if self.fail:
            raise RuntimeError("mcp down")
        return MarketSnapshot(pair=pair, ts="2026-01-01T00:00:00Z", data={"ticker": {"last": "100"}}, sources_ok=["ticker"])


def build_service(
    settings_factory,
    store,
    tmp_path,
    responses,
    *,
    mcp=None,
    readonly_ok: bool = True,
    news=None,
):
    settings = settings_factory()
    audit = AuditLog(tmp_path / "logs", 180, ())
    return LabelService(
        settings=settings,
        store=store,
        llm=StaticLLMClient(responses),
        mcp=mcp or FakeMcp(),
        news=news,
        audit=audit,
        readonly=ReadonlyCheckResult(ok=readonly_ok, status="ok" if readonly_ok else "tools_exposed"),
    )


class TestGenerateLabel:
    def test_label_written_to_store(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [label_json(pair=PAIR, action="reduce_size", multiplier=0.5)])
        label = svc.generate_label(PAIR)
        assert label.action == "reduce_size"
        assert svc.publish(label) is True
        raw = store.get(f"ai_gate:{PAIR}")
        assert raw is not None and json.loads(raw)["size_multiplier"] == 0.5

    def test_ttl_from_settings(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [label_json()])
        label = svc.generate_label(PAIR)
        assert label.ttl_sec == svc.settings.label_ttl_sec

    def test_snapshot_failure_falls_back_to_allow(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [label_json()], mcp=FakeMcp(fail=True))
        label = svc.generate_label(PAIR)
        assert label.action == "allow"

    def test_g12_retry_on_invalid_json(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, ['{"oops": 1}', label_json(pair=PAIR, action="block_new_entries")])
        label = svc.generate_label(PAIR)
        assert label.action == "block_new_entries"
        retries = [r for r in svc.audit.iter_records(stream="label") if r["event"] == "label_schema_invalid"]
        assert retries
        assert svc.llm.calls and len(svc.llm.calls) == 2

    def test_g12_all_invalid_falls_back_to_allow(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, ["nope", "still nope"])
        label = svc.generate_label(PAIR)
        assert label.action == "allow"
        assert label.evidence[0].ref == "fallback"

    def test_llm_error_falls_back(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [LLMError("boom"), LLMError("boom")])
        label = svc.generate_label(PAIR)
        assert label.action == "allow"

    def test_publish_failure(self, settings_factory, tmp_path):
        flaky = FlakyStore()
        flaky.fail_set = True
        svc = build_service(settings_factory, flaky, tmp_path, [label_json()])
        label = svc.generate_label(PAIR)
        assert svc.publish(label) is False
        assert any(r["event"] == "label_write_failed" for r in svc.audit.iter_records(stream="label"))

    def test_publish_audits(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [label_json(pair=PAIR)])
        svc.publish(svc.generate_label(PAIR))
        records = [r for r in svc.audit.iter_records(stream="label") if r["event"] == "label_written"]
        assert records
        assert records[-1]["pair"] == PAIR
        assert records[-1]["ttl_sec"] == svc.settings.label_ttl_sec


class TestRunCycle:
    def test_cycle_covers_all_pairs(self, settings_factory, store, tmp_path):
        responses = [label_json(pair=PAIR), label_json(pair="ETH/USDT")]
        svc = build_service(settings_factory, store, tmp_path, responses)
        result = svc.run_cycle()
        assert result["ok"] == 2
        assert result["failed"] == 0
        assert store.get(f"ai_gate:{PAIR}") is not None
        assert store.get("ai_gate:ETH/USDT") is not None

    def test_cycle_survives_single_pair_failure(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [label_json(pair=PAIR)])
        original = svc.generate_label

        def flaky(pair: str):
            if pair == "ETH/USDT":
                raise RuntimeError("kaboom")
            return original(pair)

        svc.generate_label = flaky  # type: ignore[assignment]
        result = svc.run_cycle()
        assert result["failed"] == 1
        assert any(r["event"] == "label_generation_error" for r in svc.audit.iter_records(stream="label"))

    def test_cycle_audits_summary(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [label_json(pair=PAIR), label_json(pair="ETH/USDT")])
        svc.run_cycle()
        records = [r for r in svc.audit.iter_records(stream="label") if r["event"] == "cycle_finished"]
        assert records and records[-1]["pairs"] == 2


class TestHeartbeatAndHealth:
    def test_heartbeat_sets_key(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [])
        assert svc.heartbeat() is True
        assert store.get("system:label_service_heartbeat") is not None
        assert store.ttl("system:label_service_heartbeat") <= svc.settings.label_heartbeat_ttl_sec

    def test_heartbeat_store_failure(self, settings_factory, tmp_path):
        flaky = FlakyStore()
        flaky.fail_set = True
        svc = build_service(settings_factory, flaky, tmp_path, [])
        assert svc.heartbeat() is False

    def test_heartbeat_age(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [])
        assert svc.heartbeat_age_sec() is None
        svc.heartbeat()
        assert svc.heartbeat_age_sec() is not None

    def test_health_ok(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [])
        payload = svc.health()
        assert payload["status"] == "ok"
        assert payload["pairs"] == ["BTC/USDT", "ETH/USDT"]

    def test_health_degraded_on_readonly_failure(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [], readonly_ok=False)
        assert svc.health()["status"] == "degraded"

    def test_health_degraded_on_redis_down(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [])
        store.set_available(False)
        assert svc.health()["status"] == "degraded"

    def test_run_forever_exits(self, settings_factory, store, tmp_path):
        svc = build_service(settings_factory, store, tmp_path, [label_json(pair=PAIR), label_json(pair="ETH/USDT")])
        stop = threading.Event()
        thread = threading.Thread(target=svc.run_forever, args=(stop,))
        thread.start()
        time.sleep(0.3)
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert svc.last_cycle_at is not None


class TestApp:
    @pytest.fixture()
    def client(self, settings_factory, store, tmp_path, monkeypatch):
        svc = build_service(settings_factory, store, tmp_path, [label_json(pair=PAIR), label_json(pair=PAIR)])
        monkeypatch.setattr(
            "aiquant.label_service.main.build_readonly_check",
            lambda settings: ReadonlyCheckResult(ok=True, status="ok", detail="stub"),
        )
        app = create_app(service=svc, settings=settings_factory())
        with TestClient(app) as c:
            yield c, svc

    def test_health_endpoint(self, client):
        c, _ = client
        assert c.get("/health").status_code == 200

    def test_metrics_endpoint(self, client):
        c, _ = client
        body = c.get("/metrics").text
        assert "aiquant_" in body

    def test_run_once(self, client):
        c, _ = client
        payload = c.post("/labels/run-once").json()
        assert payload["ok"] >= 1

    def test_read_label(self, client):
        c, _ = client
        c.post("/labels/run-once")
        payload = c.get(f"/labels/{PAIR}").json()
        assert payload["label"]["pair"] == PAIR

    def test_read_missing_label(self, client):
        c, _ = client
        response = c.get("/labels/SOL/USDT")
        assert response.status_code == 404

    def test_startup_rejected_when_readonly_fails(self, settings_factory, store, tmp_path, monkeypatch):
        svc = build_service(settings_factory, store, tmp_path, [])
        monkeypatch.setattr(
            "aiquant.label_service.main.build_readonly_check",
            lambda settings: ReadonlyCheckResult(ok=False, status="tools_exposed", forbidden=["place_order"]),
        )
        app = create_app(service=svc, settings=settings_factory())
        with pytest.raises(RuntimeError):
            with TestClient(app):
                pass
