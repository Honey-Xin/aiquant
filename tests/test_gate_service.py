"""守门服务 HTTP 层测试（鉴权、开关热生效、状态接口）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aiquant.gate.service import create_app

TOKEN = "test-token"
HEADERS = {"X-Gate-Token": TOKEN}


class StubGuarder:
    def __init__(self, resumed: bool = True, paused: bool = True):
        self.resumed = resumed
        self.paused = paused
        self.calls: list[tuple] = []
        self.started = False

    def run_forever(self, stop_event, interval=30.0):
        self.started = True
        stop_event.wait()

    def status(self):
        return {"freqtrade": {"state": "paused"}, "checks": [], "safe_to_resume": True}

    def resume_if_safe(self, force: bool = False):
        self.calls.append(("resume", force))
        return {"resumed": self.resumed, "reason": "ok" if self.resumed else "blocked", "checks": []}

    def pause(self, reason: str = "manual"):
        self.calls.append(("pause", reason))
        return {"paused": self.paused, "reason": reason}


def make_client(settings_factory, store, guarder=None, **overrides):
    settings = settings_factory(**overrides)
    app = create_app(settings=settings, store=store, guarder=guarder or StubGuarder())
    return TestClient(app), app


class TestHealthAndMetrics:
    def test_health_ok(self, settings_factory, store):
        client, _ = make_client(settings_factory, store)
        with client:
            response = client.get("/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["gate_enabled"] is True
        assert payload["components"]["redis"] == "ok"
        assert payload["halt"] is False

    def test_health_degraded_when_redis_down(self, settings_factory, store):
        store.set_available(False)
        client, _ = make_client(settings_factory, store)
        with client:
            response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_metrics_exposed(self, settings_factory, store):
        client, _ = make_client(settings_factory, store)
        with client:
            body = client.get("/metrics").text
        assert "aiquant_gate_total" in body
        assert "# TYPE" in body


class TestAuth:
    def test_missing_token(self, settings_factory, store):
        client, _ = make_client(settings_factory, store)
        with client:
            assert client.get("/gate/status").status_code == 401

    def test_wrong_token(self, settings_factory, store):
        client, _ = make_client(settings_factory, store)
        with client:
            assert client.get("/gate/status", headers={"X-Gate-Token": "nope"}).status_code == 401

    def test_missing_config_disables_api(self, settings_factory, store):
        client, _ = make_client(settings_factory, store, gate_api_token="")
        with client:
            response = client.get("/gate/status", headers={"X-Gate-Token": ""})
        assert response.status_code == 503

    def test_valid_token(self, settings_factory, store):
        client, _ = make_client(settings_factory, store)
        with client:
            response = client.get("/gate/status", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["gate_enabled"] is True


class TestGateControl:
    def test_resume_success(self, settings_factory, store):
        guarder = StubGuarder(resumed=True)
        client, _ = make_client(settings_factory, store, guarder)
        with client:
            response = client.post("/gate/resume", headers=HEADERS)
        assert response.status_code == 200
        assert guarder.calls == [("resume", False)]

    def test_resume_conflict(self, settings_factory, store):
        guarder = StubGuarder(resumed=False)
        client, _ = make_client(settings_factory, store, guarder)
        with client:
            response = client.post("/gate/resume?force=true", headers=HEADERS)
        assert response.status_code == 409
        assert guarder.calls == [("resume", True)]

    def test_pause(self, settings_factory, store):
        guarder = StubGuarder()
        client, _ = make_client(settings_factory, store, guarder)
        with client:
            response = client.post("/gate/pause?reason=manual", headers=HEADERS)
        assert response.status_code == 200
        assert guarder.calls == [("pause", "manual")]

    def test_pause_failure(self, settings_factory, store):
        guarder = StubGuarder(paused=False)
        client, _ = make_client(settings_factory, store, guarder)
        with client:
            response = client.post("/gate/pause", headers=HEADERS)
        assert response.status_code == 502

    def test_toggle_hot_applies(self, settings_factory, store):
        client, app = make_client(settings_factory, store, gate_enabled=True)
        with client:
            response = client.post("/gate/enabled?enabled=false", headers=HEADERS)
            assert response.json() == {"gate_enabled": False}
            assert store.get("system:gate_enabled") == "0"
            gate = app.state.gate
            put = gate.evaluate("BTC/USDT", 0.5)
            assert put.result == "disabled"
            records = [
                r for r in app.state.audit.iter_records(stream="gate") if r["event"] == "gate_toggle_changed"
            ]
            assert records and records[-1]["source"] == "api"

            client.post("/gate/enabled?enabled=true", headers=HEADERS)
            assert store.get("system:gate_enabled") == "1"

    def test_lifespan_starts_guarder(self, settings_factory, store):
        guarder = StubGuarder()
        client, _ = make_client(settings_factory, store, guarder)
        with client:
            assert guarder.started is True
