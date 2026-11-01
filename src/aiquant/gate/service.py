"""守门服务 HTTP 入口（FR-2.5 / §6.3）。

接口：
  * GET  /health          —— 本服务健康（Redis、halt、闸门开关）
  * GET  /metrics         —— Prometheus 文本格式
  * GET  /gate/status     —— Freqtrade 状态 + 三项检查 + 是否可安全恢复
  * POST /gate/resume     —— 通过检查后恢复交易（写审计）
  * POST /gate/pause      —— 暂停交易（写审计）

鉴权：除 /health 与 /metrics 外，必须带 ``X-Gate-Token``。未配置 token 时接口
直接拒绝，避免裸奔。
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from aiquant import metrics as M
from aiquant.audit.logger import AuditLog
from aiquant.logging_setup import get_logger, setup_logging
from aiquant.settings import Settings, get_settings

from .decision import Gate, GateToggle
from .guarder import Guarder, make_guarder
from .health import build_health

log = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    store=None,
    gate: Gate | None = None,
    guarder: Guarder | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    audit = AuditLog(settings.audit_log_dir, settings.audit_retention_days, settings.secrets())

    from aiquant.label_service.store import build_store  # 局部导入

    store = store if store is not None else build_store(settings.redis_url)
    toggle = GateToggle(store=store, default=settings.gate_enabled, audit=audit)
    gate = gate or Gate(
        store=store,
        toggle=toggle,
        audit=audit,
        min_amounts=settings.min_amounts,
        fail_open=settings.gate_fail_open,
    )
    guarder = guarder or make_guarder(settings, store, audit)
    stop_event = threading.Event()
    poller: threading.Thread | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal poller
        audit.write("guarder", "gate_service_started", gate_enabled=settings.gate_enabled)
        poller = threading.Thread(
            target=guarder.run_forever, args=(stop_event, 30.0), daemon=True, name="guarder-poller"
        )
        poller.start()
        try:
            yield
        finally:
            stop_event.set()
            if poller is not None:
                poller.join(timeout=5)
            audit.write("guarder", "gate_service_stopped")

    app = FastAPI(title="AI Quant Gate Service", version="1.0.0", lifespan=lifespan)
    app.state.gate = gate
    app.state.guarder = guarder
    app.state.settings = settings
    app.state.audit = audit

    def require_token(x_gate_token: str | None = Header(default=None)) -> None:
        expected = settings.gate_api_token
        if not expected:
            raise HTTPException(status_code=503, detail="GATE_API_TOKEN 未配置，接口已关闭")
        if x_gate_token != expected:
            raise HTTPException(status_code=401, detail="invalid gate token")

    @app.get("/health")
    def health() -> JSONResponse:
        report = build_health(gate, toggle)
        return JSONResponse(report.to_dict(), status_code=report.http_status)

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        return Response(content=M.render_metrics(), media_type="text/plain; version=0.0.4")

    @app.get("/gate/status", dependencies=[Depends(require_token)])
    def gate_status() -> JSONResponse:
        payload = guarder.status()
        payload["gate_enabled"] = toggle.is_enabled()
        return JSONResponse(payload)

    @app.post("/gate/resume", dependencies=[Depends(require_token)])
    def gate_resume(force: bool = False) -> JSONResponse:
        result = guarder.resume_if_safe(force=force)
        code = 200 if result.get("resumed") else 409
        return JSONResponse(result, status_code=code)

    @app.post("/gate/pause", dependencies=[Depends(require_token)])
    def gate_pause(reason: str = "manual") -> JSONResponse:
        result = guarder.pause(reason=reason)
        return JSONResponse(result, status_code=200 if result.get("paused") else 502)

    @app.post("/gate/enabled", dependencies=[Depends(require_token)])
    def set_gate_enabled(enabled: bool) -> JSONResponse:
        """通过 Redis 覆盖闸门开关，热生效，不重启进程（FR-2.6）。"""
        store.setex("system:gate_enabled", 3600 * 24 * 365, "1" if enabled else "0")
        audit.write("gate", "gate_toggle_changed", enabled=enabled, source="api")
        return JSONResponse({"gate_enabled": enabled})

    return app


def main() -> None:  # pragma: no cover - uvicorn 入口
    import uvicorn

    settings = get_settings()
    setup_logging("INFO", settings.secrets())
    application = create_app(settings=settings)
    uvicorn.run(
        application,
        host=settings.gate_host,
        port=settings.gate_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
