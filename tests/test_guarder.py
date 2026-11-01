"""守门服务测试（G-07：fail-closed；G-11：审计）。"""

from __future__ import annotations

import httpx
import pytest

from aiquant.audit.logger import AuditLog
from aiquant.gate.freqtrade_client import FreqtradeClient
from aiquant.gate.guarder import Guarder, make_guarder


def freqtrade_client(state: str = "paused", *, start_status: int = 200, calls: list[str] | None = None):
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/show_config":
            return httpx.Response(200, json={"state": state})
        if path == "/api/v1/start":
            calls.append("start")
            if start_status != 200:
                return httpx.Response(start_status, json={"error": "boom"})
            return httpx.Response(200, json={"status": "started"})
        if path == "/api/v1/stop":
            calls.append("stop")
            return httpx.Response(200, json={"status": "stopped"})
        return httpx.Response(404)

    client = FreqtradeClient("http://ft.test:8082", "u", "p", transport=httpx.MockTransport(handler))
    return client, calls


def label_transport(healthy: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            if not healthy:
                return httpx.Response(503, json={"status": "degraded"})
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def build_guarder(store, tmp_path, *, healthy: bool = True, state: str = "paused", required: bool = True):
    ft, calls = freqtrade_client(state)
    audit = AuditLog(tmp_path / "logs", 180, ())
    guarder = Guarder(
        freqtrade=ft,
        store=store,
        label_service_url="http://label.test:8081",
        audit=audit,
        transport=label_transport(healthy),
    )
    return guarder, calls, audit


class TestChecks:
    def test_label_service_ok(self, store, tmp_path):
        guarder, _, _ = build_guarder(store, tmp_path, healthy=True)
        assert guarder.check_label_service().ok is True

    def test_label_service_down(self, store, tmp_path):
        guarder, _, _ = build_guarder(store, tmp_path, healthy=False)
        result = guarder.check_label_service()
        assert result.ok is False
        assert "503" in result.detail

    def test_label_service_unreachable(self, store, tmp_path):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        ft, _ = freqtrade_client()
        guarder = Guarder(
            freqtrade=ft,
            store=store,
            label_service_url="http://label.test:8081",
            audit=AuditLog(tmp_path / "logs", 180, ()),
            transport=httpx.MockTransport(boom),
        )
        result = guarder.check_label_service()
        assert result.ok is False
        assert "unreachable" in result.detail

    def test_redis_check(self, store, tmp_path):
        guarder, _, _ = build_guarder(store, tmp_path)
        assert guarder.check_redis().ok is True
        store.set_available(False)
        assert guarder.check_redis().ok is False

    def test_halt_check(self, store, tmp_path):
        guarder, _, _ = build_guarder(store, tmp_path)
        assert guarder.check_halt().ok is True
        store.setex("system:halt", 60, "1")
        assert guarder.check_halt().ok is False


class TestResume:
    def test_g07_resume_when_all_checks_pass(self, store, tmp_path):
        guarder, calls, audit = build_guarder(store, tmp_path)
        result = guarder.resume_if_safe()
        assert result["resumed"] is True
        assert calls == ["start"]
        events = [r["event"] for r in audit.iter_records(stream="guarder")]
        assert "resume" in events

    def test_g07_keeps_paused_when_label_service_down(self, store, tmp_path):
        guarder, calls, audit = build_guarder(store, tmp_path, healthy=False)
        result = guarder.resume_if_safe()
        assert result["resumed"] is False
        assert calls == []
        assert any(r["event"] == "resume_refused" for r in audit.iter_records(stream="guarder"))

    def test_g07_keeps_paused_when_halt(self, store, tmp_path):
        guarder, calls, _ = build_guarder(store, tmp_path)
        store.setex("system:halt", 60, "1")
        result = guarder.resume_if_safe()
        assert result["resumed"] is False
        assert calls == []
        assert "system:halt" in result["reason"]

    def test_g07_keeps_paused_when_redis_down(self, store, tmp_path):
        guarder, calls, _ = build_guarder(store, tmp_path)
        store.set_available(False)
        result = guarder.resume_if_safe()
        assert result["resumed"] is False
        assert calls == []

    def test_already_running_is_noop(self, store, tmp_path):
        guarder, calls, _ = build_guarder(store, tmp_path, state="running")
        result = guarder.resume_if_safe()
        assert result["resumed"] is False
        assert result["reason"] == "already running"
        assert calls == []

    def test_resume_failure_is_audited(self, store, tmp_path):
        ft, calls = freqtrade_client(start_status=500)
        audit = AuditLog(tmp_path / "logs", 180, ())
        guarder = Guarder(ft, store, "http://label.test:8081", audit=audit, transport=label_transport(True))
        result = guarder.resume_if_safe()
        assert result["resumed"] is False
        assert calls == ["start"]
        assert any(r["event"] == "resume_failed" for r in audit.iter_records(stream="guarder"))

    def test_pause(self, store, tmp_path):
        guarder, calls, audit = build_guarder(store, tmp_path)
        result = guarder.pause("operator")
        assert result["paused"] is True
        assert calls == ["stop"]
        assert any(r["event"] == "pause" for r in audit.iter_records(stream="guarder"))

    def test_status_payload(self, store, tmp_path):
        guarder, _, _ = build_guarder(store, tmp_path)
        payload = guarder.status()
        assert payload["freqtrade"]["state"] == "paused"
        assert payload["safe_to_resume"] is True
        assert len(payload["checks"]) == 3


class TestPolling:
    def test_run_forever_stops(self, store, tmp_path):
        import threading

        guarder, calls, _ = build_guarder(store, tmp_path)
        stop = threading.Event()
        thread = threading.Thread(target=guarder.run_forever, args=(stop, 0.01))
        thread.start()
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_make_guarder(self, settings_factory, store, tmp_path):
        settings = settings_factory()
        guarder = make_guarder(settings, store, AuditLog(tmp_path / "logs", 180, ()))
        assert isinstance(guarder, Guarder)
        assert guarder.label_service_url == "http://label.test:8081"
