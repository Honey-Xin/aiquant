"""健康与就绪聚合（/health 内容构造）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .decision import Gate, GateToggle


@dataclass
class HealthReport:
    status: str
    components: dict[str, Any] = field(default_factory=dict)
    gate_enabled: bool = True
    halt: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "components": self.components,
            "gate_enabled": self.gate_enabled,
            "halt": self.halt,
        }

    @property
    def http_status(self) -> int:
        return 200 if self.status == "ok" else 503


def build_health(gate: Gate, toggle: GateToggle | None = None, freqtrade_state: str | None = None) -> HealthReport:
    """闸门可用性 = Redis 可用 + 无 halt（闸门关闭不算不健康，只算降级）。"""
    components: dict[str, Any] = {}
    redis_ok = bool(gate.store.ping())
    components["redis"] = "ok" if redis_ok else "down"

    halt = gate.halt_active()
    components["system_halt"] = halt

    enabled = (toggle or gate.toggle).is_enabled()
    components["gate_enabled"] = enabled

    if freqtrade_state:
        components["freqtrade"] = freqtrade_state

    healthy = redis_ok and not halt
    status = "ok" if healthy else "degraded"
    return HealthReport(status=status, components=components, gate_enabled=enabled, halt=halt)
