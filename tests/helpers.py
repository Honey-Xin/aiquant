"""测试辅助：构造合法标签、可注入故障的存储。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from aiquant.label_service.schema import Evidence, Label
from aiquant.label_service.store.base import StoreError
from aiquant.label_service.store.memory_store import MemoryStore


def make_label(
    pair: str = "BTC/USDT",
    action: str = "allow",
    multiplier: float | None = None,
    ttl_sec: int = 300,
    now: datetime | None = None,
    regime: str = "risk_on",
    confidence: str = "medium",
    evidence: list[Evidence] | None = None,
    flags: list[str] | None = None,
) -> Label:
    ts = now or datetime.now(timezone.utc)
    if multiplier is None:
        multiplier = 0.5 if action == "reduce_size" else 1.0
    return Label(
        pair=pair,
        generated_at=ts,
        ttl_sec=ttl_sec,
        regime=regime,
        event_flags=list(flags or []),
        action=action,
        size_multiplier=multiplier,
        confidence=confidence,
        evidence=evidence or [],
    )


def label_json(**kwargs: Any) -> str:
    return make_label(**kwargs).to_json()


def label_dict(**kwargs: Any) -> dict[str, Any]:
    return json.loads(label_json(**kwargs))


class FlakyStore(MemoryStore):
    """可模拟 Redis 抛出 StoreError 的存储。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_get = False
        self.fail_set = False

    def get(self, key: str) -> str | None:
        if self.fail_get:
            raise StoreError("simulated redis outage")
        return super().get(key)

    def setex(self, key: str, ttl_sec: int, value: str) -> None:
        if self.fail_set:
            raise StoreError("simulated redis write failure")
        super().setex(key, ttl_sec, value)


def put_label(store: MemoryStore, label: Label) -> None:
    store.setex(f"ai_gate:{label.pair}", label.ttl_sec, label.to_json())


def expiry_kwargs(seconds_ago: int) -> dict[str, Any]:
    return {"now": datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)}
