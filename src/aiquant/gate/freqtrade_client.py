"""Freqtrade REST 客户端（暂停 / 恢复 / 状态）。

守门服务通过它做 fail-closed 恢复；任何 HTTP 失败都当作"不可恢复"，保持暂停。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from aiquant.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class FreqtradeState:
    reachable: bool
    state: str  # running | paused | unknown
    detail: str = ""

    @property
    def paused(self) -> bool:
        return self.state == "paused"


class FreqtradeClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout_sec: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_sec = timeout_sec
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            auth=(self.username, self.password) if self.username else None,
            timeout=self.timeout_sec,
            transport=self._transport,
        )

    def state(self) -> FreqtradeState:
        try:
            with self._client() as client:
                resp = client.get("/api/v1/show_config")
            if resp.status_code >= 400:
                return FreqtradeState(False, "unknown", f"HTTP {resp.status_code}")
            data: Any = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return FreqtradeState(False, "unknown", type(exc).__name__)
        raw_state = str(data.get("state", "")).lower() if isinstance(data, dict) else ""
        if raw_state not in {"running", "paused"}:
            return FreqtradeState(True, "unknown", f"state={raw_state!r}")
        return FreqtradeState(True, raw_state)

    def _post(self, path: str) -> tuple[bool, int, str]:
        try:
            with self._client() as client:
                resp = client.post(path)
        except httpx.HTTPError as exc:
            return False, 0, type(exc).__name__
        body = ""
        try:
            payload = resp.json()
            body = str(payload)[:200] if payload is not None else ""
        except ValueError:
            body = resp.text[:200]
        return resp.status_code < 400, resp.status_code, body

    def resume(self) -> tuple[bool, str]:
        ok, code, body = self._post("/api/v1/start")
        if ok:
            return True, "resumed"
        log.error("Freqtrade 恢复失败 HTTP %s: %s", code, body)
        return False, f"HTTP {code}: {body}"

    def pause(self) -> tuple[bool, str]:
        ok, code, body = self._post("/api/v1/stop")
        if ok:
            return True, "paused"
        log.error("Freqtrade 暂停失败 HTTP %s: %s", code, body)
        return False, f"HTTP {code}: {body}"
