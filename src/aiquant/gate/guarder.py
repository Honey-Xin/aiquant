"""守门服务：fail-closed 启动（FR-2.5）。

Freqtrade 以 ``initial_state=paused`` 启动。本服务只有在下面三条同时满足时才
调用 Freqtrade API 恢复交易：

  1. 标签服务 ``/health`` 返回 200 且 status=ok；
  2. Redis 可用（ping）；
  3. 不存在 ``system:halt=1``。

任一失败 → 保持暂停 + 告警。恢复动作写审计日志。默认每 30 秒复查一次：
交易已在运行时不做任何事，只有"暂停中且检查通过"才恢复。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from aiquant import metrics as M
from aiquant.audit.logger import AuditLog
from aiquant.logging_setup import get_logger

from .decision import HALT_KEY
from .freqtrade_client import FreqtradeClient, FreqtradeState

log = get_logger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


class Guarder:
    def __init__(
        self,
        freqtrade: FreqtradeClient,
        store,
        label_service_url: str,
        audit: AuditLog | None = None,
        http_timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.freqtrade = freqtrade
        self.store = store
        self.label_service_url = label_service_url.rstrip("/")
        self.audit = audit or AuditLog()
        self.http_timeout = http_timeout
        self._transport = transport

    # ------------------------------------------------------------- 检查项
    def check_label_service(self) -> CheckResult:
        url = f"{self.label_service_url}/health"
        try:
            with httpx.Client(timeout=self.http_timeout, transport=self._transport) as client:
                resp = client.get(url)
        except httpx.HTTPError as exc:
            return CheckResult("label_service", False, f"unreachable: {type(exc).__name__}")
        if resp.status_code >= 400:
            return CheckResult("label_service", False, f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            return CheckResult("label_service", False, "invalid json")
        status = str(payload.get("status", "")).lower() if isinstance(payload, dict) else ""
        if status != "ok":
            return CheckResult("label_service", False, f"status={status or 'unknown'}")
        return CheckResult("label_service", True, "ok")

    def check_redis(self) -> CheckResult:
        ok = bool(self.store.ping())
        return CheckResult("redis", ok, "ok" if ok else "ping failed")

    def check_halt(self) -> CheckResult:
        try:
            raw = self.store.get(HALT_KEY)
        except Exception as exc:  # noqa: BLE001 - 存储异常视为有 halt（保守）
            return CheckResult("halt", False, f"halt unknown: {type(exc).__name__}")
        active = raw is not None and str(raw).strip() in {"1", "true", "True"}
        M.SYSTEM_HALT.set(1.0 if active else 0.0)
        if active:
            return CheckResult("halt", False, "system:halt=1")
        return CheckResult("halt", True, "no halt")

    def run_checks(self) -> list[CheckResult]:
        started = time.perf_counter()
        checks = [self.check_label_service(), self.check_redis(), self.check_halt()]
        M.GUARDER_CHECK_SECONDS.observe(time.perf_counter() - started)
        return checks

    # ------------------------------------------------------------- 决策
    def resume_if_safe(self, force: bool = False) -> dict[str, Any]:
        checks = self.run_checks()
        failed = [c for c in checks if not c.ok]
        state: FreqtradeState = self.freqtrade.state()
        M.FREQTRADE_PAUSED.set(1.0 if state.paused else 0.0)

        if failed:
            detail = "; ".join(f"{c.name}: {c.detail}" for c in failed)
            self.audit.write(
                "guarder",
                "resume_refused",
                reasons=detail,
                checks=[c.to_dict() for c in checks],
                freqtrade_state=state.state,
            )
            log.warning("拒绝恢复交易: %s", detail)
            return {"resumed": False, "reason": detail, "checks": [c.to_dict() for c in checks]}

        if not force and state.reachable and not state.paused:
            return {
                "resumed": False,
                "reason": "already running",
                "checks": [c.to_dict() for c in checks],
            }

        ok, detail = self.freqtrade.resume()
        self.audit.write(
            "guarder",
            "resume" if ok else "resume_failed",
            detail=detail,
            checks=[c.to_dict() for c in checks],
            freqtrade_state=state.state,
        )
        M.FREQTRADE_PAUSED.set(0.0 if ok else 1.0)
        return {"resumed": ok, "reason": detail, "checks": [c.to_dict() for c in checks]}

    def pause(self, reason: str = "manual") -> dict[str, Any]:
        ok, detail = self.freqtrade.pause()
        self.audit.write("guarder", "pause", reason=reason, ok=ok, detail=detail)
        M.FREQTRADE_PAUSED.set(1.0 if ok else 0.0)
        return {"paused": ok, "detail": detail}

    def status(self) -> dict[str, Any]:
        state = self.freqtrade.state()
        M.FREQTRADE_PAUSED.set(1.0 if state.paused else 0.0)
        checks = self.run_checks()
        return {
            "freqtrade": {
                "reachable": state.reachable,
                "state": state.state,
                "detail": state.detail,
            },
            "checks": [c.to_dict() for c in checks],
            "safe_to_resume": all(c.ok for c in checks),
        }

    # ------------------------------------------------------------- 轮询
    def run_forever(self, stop_event: threading.Event, interval: float = 30.0) -> None:
        while not stop_event.is_set():
            try:
                self.resume_if_safe()
            except Exception as exc:  # noqa: BLE001 - 轮询线程不得退出
                log.error("守门轮询异常: %s", type(exc).__name__)
                self.audit.write("guarder", "error", error=type(exc).__name__)
            stop_event.wait(timeout=interval)


def make_guarder(settings, store, audit: AuditLog | None = None) -> Guarder:
    client = FreqtradeClient(
        settings.freqtrade_api_url,
        settings.freqtrade_api_user,
        settings.freqtrade_api_password,
    )
    return Guarder(
        freqtrade=client,
        store=store,
        label_service_url=settings.label_service_url,
        audit=audit or AuditLog(settings.audit_log_dir, settings.audit_retention_days, settings.secrets()),
    )


__all__ = ["CheckResult", "Guarder", "FreqtradeState", "make_guarder", "Callable"]
