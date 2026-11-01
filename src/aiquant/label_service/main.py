"""AI 标签服务（研究域）。

职责边界（§3.1）：
  * 只读：行情经 ``okx-trade-mcp --read-only`` 获取，启动自检不通过就拒绝启动；
  * 只写标签：写入 Redis ``ai_gate:{pair}``，TTL 与标签内 ttl_sec 一致；
  * 永不交易：本模块不 import 任何下单代码，也不接触 OKX 私钥。

故障语义：LLM/数据源/存储任何异常 → 写 ``action=allow`` 兜底标签 + 记录错误，
执行域因此永远不会因为标签服务故障而被阻塞（§7 可靠性）。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from aiquant import metrics as M
from aiquant.audit.logger import AuditLog
from aiquant.logging_setup import get_logger, setup_logging
from aiquant.settings import Settings, get_settings

from .llm.client import HttpLLMClient, LLMClient, LLMError
from .llm.prompts import SYSTEM_PROMPT, build_retry_prompt, build_user_prompt
from .readonly_check import ReadonlyCheckResult, run_readonly_check
from .schema import Label, fallback_allow_label, parse_with_retry
from .sources.news import NewsClient, NewsItem
from .sources.okx_mcp import MarketSnapshot, OkxMcpReadOnlyClient
from .store.base import LabelStore, StoreError
from .store.memory_store import MemoryStore

log = get_logger(__name__)

LABEL_KEY_TEMPLATE = "ai_gate:{pair}"
HEARTBEAT_KEY = "system:label_service_heartbeat"


def _utc_iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LabelService:
    """标签生成器：可单独构造、单独单测，不依赖 FastAPI。"""

    def __init__(
        self,
        settings: Settings,
        store: LabelStore,
        llm: LLMClient,
        mcp: OkxMcpReadOnlyClient,
        news: NewsClient | None = None,
        audit: AuditLog | None = None,
        readonly: ReadonlyCheckResult | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.llm = llm
        self.mcp = mcp
        self.news = news
        self.audit = audit or AuditLog(settings.audit_log_dir, settings.audit_retention_days, settings.secrets())
        self.readonly = readonly or ReadonlyCheckResult(ok=False, status="not_run")
        self.started_at = datetime.now(timezone.utc)
        self.last_cycle_at: str | None = None
        self.last_cycle_ok = 0
        self.last_cycle_failed = 0
        self._last_heartbeat_monotonic: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 生成
    def build_snapshot(self, pair: str) -> tuple[MarketSnapshot, list[NewsItem]]:
        snapshot = self.mcp.snapshot(pair)
        news_items = self.news.fetch(pair) if self.news is not None else []
        return snapshot, news_items

    def generate_label(self, pair: str) -> Label:
        ttl = self.settings.label_ttl_sec
        started = time.perf_counter()
        try:
            snapshot, news_items = self.build_snapshot(pair)
        except Exception as exc:  # noqa: BLE001 - 数据源异常不能阻塞标签生成
            log.warning("数据源异常 pair=%s: %s", pair, type(exc).__name__)
            snapshot = MarketSnapshot(pair=pair, ts=_utc_iso(), errors=[f"snapshot error: {type(exc).__name__}"])
            news_items = []

        user_prompt = build_user_prompt(
            pair=pair,
            snapshot=snapshot.to_prompt_payload(),
            news=[n.to_prompt_payload() for n in news_items],
            now_iso=_utc_iso(),
            ttl_sec=ttl,
        )
        state: dict[str, str | None] = {"raw": None, "error": ""}

        def _gen() -> str:
            if state["raw"] is None:
                prompt = user_prompt
            else:
                prompt = build_retry_prompt(user_prompt, str(state["raw"]), str(state["error"]))
            raw = self.llm.complete_json(SYSTEM_PROMPT, prompt)
            state["raw"] = raw
            return raw

        def _on_retry(attempt: int, error: str) -> None:
            state["error"] = error
            M.LABEL_SCHEMA_RETRY.inc()
            M.LABEL_SCHEMA_INVALID.inc()
            self.audit.write(
                "label",
                "label_schema_invalid",
                pair=pair,
                attempt=attempt,
                error=error,
            )
            log.warning("标签 Schema 校验失败，重试 %s: %s", attempt, error)

        label, attempts = parse_with_retry(
            _gen, pair, ttl_sec=ttl, retries=self.settings.label_schema_retry, on_retry=_on_retry
        )
        elapsed = time.perf_counter() - started
        M.LABEL_GENERATION_SECONDS.observe(elapsed)

        if attempts > 1 and label.evidence and label.evidence[0].ref == "fallback":
            M.LABEL_GENERATION_FAILURES.inc()
            M.LABEL_GENERATION_TOTAL.inc(status="failed")
            self.audit.write(
                "label",
                "label_fallback_allow",
                pair=pair,
                attempts=attempts,
                reason=str(state["error"] or "unknown"),
                elapsed_sec=round(elapsed, 4),
            )
        else:
            M.LABEL_GENERATION_TOTAL.inc(status="ok")
        return label

    # ------------------------------------------------------------- 写标签
    def publish(self, label: Label) -> bool:
        key = LABEL_KEY_TEMPLATE.format(pair=label.pair)
        payload = label.to_json()
        try:
            self.store.setex(key, label.ttl_sec, payload)
        except StoreError as exc:
            M.LABEL_GENERATION_FAILURES.inc()
            self.audit.write("label", "label_write_failed", pair=label.pair, error=str(exc))
            log.error("标签写入失败 pair=%s: %s", label.pair, type(exc).__name__)
            return False
        M.LABEL_WRITTEN.inc(pair=label.pair)
        self.audit.write(
            "label",
            "label_written",
            pair=label.pair,
            action=label.action,
            regime=label.regime,
            size_multiplier=label.size_multiplier,
            confidence=label.confidence,
            generated_at=_utc_iso(label.generated_at),
            ttl_sec=label.ttl_sec,
            evidence_count=len(label.evidence),
        )
        return True

    # ------------------------------------------------------------- 一轮
    def run_cycle(self) -> dict[str, Any]:
        ok = 0
        failed = 0
        started = time.perf_counter()
        for pair in self.settings.pairs:
            try:
                label = self.generate_label(pair)
            except Exception as exc:  # noqa: BLE001 - 单对失败不影响其它对
                failed += 1
                M.LABEL_GENERATION_FAILURES.inc()
                M.LABEL_GENERATION_TOTAL.inc(status="failed")
                log.error("标签生成异常 pair=%s: %s", pair, type(exc).__name__)
                self.audit.write("label", "label_generation_error", pair=pair, error=type(exc).__name__)
                continue
            if self.publish(label):
                ok += 1
            else:
                failed += 1
        elapsed = time.perf_counter() - started
        with self._lock:
            self.last_cycle_at = _utc_iso()
            self.last_cycle_ok = ok
            self.last_cycle_failed = failed
        self.audit.write(
            "label",
            "cycle_finished",
            ok=ok,
            failed=failed,
            elapsed_sec=round(elapsed, 3),
            pairs=len(self.settings.pairs),
        )
        return {"ok": ok, "failed": failed, "elapsed_sec": round(elapsed, 3)}

    # ------------------------------------------------------------- 心跳
    def heartbeat(self) -> bool:
        try:
            self.store.setex(
                HEARTBEAT_KEY,
                self.settings.label_heartbeat_ttl_sec,
                _utc_iso(),
            )
        except StoreError as exc:
            log.error("心跳写入失败: %s", type(exc).__name__)
            return False
        self._last_heartbeat_monotonic = time.monotonic()
        M.LABEL_HEARTBEAT_AGE.set(0.0)
        return True

    def heartbeat_age_sec(self) -> float | None:
        if self._last_heartbeat_monotonic is None:
            return None
        age = time.monotonic() - self._last_heartbeat_monotonic
        M.LABEL_HEARTBEAT_AGE.set(age)
        return round(age, 1)

    # ------------------------------------------------------------- 健康
    def health(self) -> dict[str, Any]:
        redis_ok = self.store.ping()
        readonly_status = self.readonly.to_dict()
        healthy = redis_ok and bool(readonly_status.get("ok"))
        return {
            "status": "ok" if healthy else "degraded",
            "started_at": _utc_iso(self.started_at),
            "redis": "ok" if redis_ok else "down",
            "readonly_check": readonly_status,
            "pairs": self.settings.pairs,
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_ok": self.last_cycle_ok,
            "last_cycle_failed": self.last_cycle_failed,
            "heartbeat_age_sec": self.heartbeat_age_sec(),
            "cycle_sec": self.settings.label_cycle_sec,
            "ttl_sec": self.settings.label_ttl_sec,
            "llm_provider": self.settings.llm_provider,
            "llm_model": self.settings.llm_model,
        }

    # ------------------------------------------------------------- 调度
    def run_forever(self, stop_event: threading.Event) -> None:
        next_cycle = 0.0
        next_heartbeat = 0.0
        while not stop_event.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                self.heartbeat()
                next_heartbeat = now + self.settings.label_heartbeat_sec
            if now >= next_cycle:
                self.run_cycle()
                next_cycle = time.monotonic() + self.settings.label_cycle_sec
            stop_event.wait(timeout=1.0)


def build_readonly_check(settings: Settings) -> ReadonlyCheckResult:
    result = run_readonly_check(
        cmd=settings.okx_mcp_readonly_cmd,
        timeout_sec=15.0,
        required=settings.readonly_check_required,
    )
    M.READONLY_CHECK_OK.set(1.0 if result.ok else 0.0)
    return result


def build_service(settings: Settings | None = None) -> LabelService:
    settings = settings or get_settings()
    llm = HttpLLMClient(
        provider=settings.llm_provider,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout_sec=settings.llm_timeout_sec,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
    )
    mcp = OkxMcpReadOnlyClient(cmd=settings.okx_mcp_readonly_cmd, timeout_sec=15.0)
    news = NewsClient(
        enabled=settings.news_enabled,
        url=settings.news_api_url,
        token=settings.news_api_token,
        timeout_sec=settings.news_timeout_sec,
        max_items=settings.news_max_items,
    )
    from .store import build_store  # 局部导入，避免测试时的重依赖

    store = build_store(settings.redis_url)
    audit = AuditLog(settings.audit_log_dir, settings.audit_retention_days, settings.secrets())
    return LabelService(settings, store, llm, mcp, news, audit)


def create_app(service: LabelService | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    stop_event = threading.Event()
    scheduler: threading.Thread | None = None
    holder: dict[str, LabelService] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal scheduler
        svc = service or build_service(settings)
        holder["service"] = svc

        # FR-1.4：只读自检失败 → 拒绝启动
        result = build_readonly_check(settings)
        svc.readonly = result
        svc.audit.write(
            "readonly",
            "readonly_check",
            status=result.status,
            ok=result.ok,
            tools=result.tools,
            forbidden=result.forbidden,
            detail=result.detail,
        )
        M.READONLY_CHECK_OK.set(1.0 if result.ok else 0.0)
        if not result.ok:
            raise RuntimeError(
                f"只读自检未通过（status={result.status}），标签服务拒绝启动: {result.detail}"
            )
        log.info("只读自检通过: %s", result.detail)

        svc.heartbeat()
        scheduler = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True, name="label-scheduler")
        scheduler.start()
        try:
            yield
        finally:
            stop_event.set()
            if scheduler is not None:
                scheduler.join(timeout=5)
            svc.audit.write("label", "label_service_stopped")

    app = FastAPI(title="AI Quant Label Service", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> JSONResponse:
        svc = holder.get("service")
        if svc is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        payload = svc.health()
        code = 200 if payload["status"] == "ok" else 503
        return JSONResponse(payload, status_code=code)

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        return Response(content=M.render_metrics(), media_type="text/plain; version=0.0.4")

    @app.post("/labels/run-once")
    def run_once() -> JSONResponse:
        svc = holder.get("service")
        if svc is None:
            return JSONResponse({"error": "not ready"}, status_code=503)
        return JSONResponse(svc.run_cycle())

    @app.get("/labels/{pair:path}")
    def read_label(pair: str) -> JSONResponse:
        svc = holder.get("service")
        if svc is None:
            return JSONResponse({"error": "not ready"}, status_code=503)
        raw = svc.store.get(LABEL_KEY_TEMPLATE.format(pair=pair))
        if raw is None:
            return JSONResponse({"pair": pair, "label": None}, status_code=404)
        try:
            return JSONResponse({"pair": pair, "label": json.loads(raw)})
        except json.JSONDecodeError:
            return JSONResponse({"pair": pair, "label": None, "raw": raw})

    return app


app = None  # uvicorn --factory 或下面的 main() 使用


def main() -> None:  # pragma: no cover - uvicorn 入口
    import uvicorn

    settings = get_settings()
    setup_logging("INFO", settings.secrets())
    application = create_app(settings=settings)
    uvicorn.run(
        application,
        host=settings.label_host,
        port=settings.label_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
