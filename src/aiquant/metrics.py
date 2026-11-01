"""零依赖的 Prometheus 文本格式指标实现。

为什么自己写：执行域与标签域都要暴露 /metrics，但引入 prometheus_client 会把
一个高频变更的第三方库塞进实盘链路。这里只需要 counter / gauge / histogram
三类原语，自己实现可以保证行为稳定、可单测、无依赖。
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Iterable, Sequence

Labels = tuple[tuple[str, str], ...]


def _fmt(value: float) -> str:
    if value == math.inf:
        return "+Inf"
    if value == -math.inf:
        return "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _labels_to_str(labels: Labels, extra: Labels = ()) -> str:
    items = labels + extra
    if not items:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in items)
    return "{" + inner + "}"


def _escape(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Metric:
    kind = "untyped"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str]) -> Labels:
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def render(self) -> Iterable[str]:  # pragma: no cover - overridden
        return ()


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[Labels, float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._values[self._key(labels)] += float(amount)

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            snapshot = dict(self._values)
        if not snapshot:
            yield f"{self.name} 0"
            return
        for labels, val in snapshot.items():
            yield f"{self.name}{_labels_to_str(labels)} {_fmt(val)}"


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[Labels, float] = defaultdict(float)

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._values[self._key(labels)] = float(value)

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} gauge"
        with self._lock:
            snapshot = dict(self._values)
        if not snapshot:
            yield f"{self.name} 0"
            return
        for labels, val in snapshot.items():
            yield f"{self.name}{_labels_to_str(labels)} {_fmt(val)}"


class Histogram(_Metric):
    kind = "histogram"

    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(float(b) for b in buckets))
        self._counts: dict[Labels, list[float]] = {}
        self._sum: dict[Labels, float] = defaultdict(float)
        self._count: dict[Labels, float] = defaultdict(float)

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = [0.0] * len(self.buckets)
                self._counts[key] = counts
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1.0
            self._sum[key] += float(value)
            self._count[key] += 1.0

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            counts = {k: list(v) for k, v in self._counts.items()}
            sums = dict(self._sum)
            total = dict(self._count)
        for key, buckets in counts.items():
            cumulative = 0.0
            for bound, cnt in zip(self.buckets, buckets):
                cumulative += cnt
                yield (
                    f"{self.name}_bucket{_labels_to_str(key, (('le', _fmt(bound)),))} "
                    f"{_fmt(cumulative)}"
                )
            yield f"{self.name}_bucket{_labels_to_str(key, (('le', '+Inf'),))} {_fmt(total[key])}"
            yield f"{self.name}_sum{_labels_to_str(key)} {_fmt(sums[key])}"
            yield f"{self.name}_count{_labels_to_str(key)} {_fmt(total[key])}"


class Registry:
    def __init__(self) -> None:
        self._metrics: list[_Metric] = []

    def register(self, metric: _Metric) -> _Metric:
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        lines: list[str] = []
        for m in self._metrics:
            lines.extend(m.render())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        self._metrics.clear()


# ---------------------------------------------------------------- 全局指标集
REGISTRY = Registry()

# 标签域
LABEL_GENERATION_TOTAL = REGISTRY.register(
    Counter("aiquant_label_generation_total", "标签生成次数（ok/failed）", ("status",))
)
LABEL_GENERATION_FAILURES = REGISTRY.register(
    Counter("aiquant_label_generation_failures_total", "标签生成失败次数（含 LLM 与数据源）")
)
LABEL_SCHEMA_INVALID = REGISTRY.register(
    Counter("aiquant_label_schema_invalid_total", "LLM 输出未通过 Schema 校验次数")
)
LABEL_SCHEMA_RETRY = REGISTRY.register(
    Counter("aiquant_label_schema_retry_total", "Schema 校验失败后的重试次数")
)
LABEL_WRITTEN = REGISTRY.register(
    Counter("aiquant_label_written_total", "成功写入 Redis 的标签数", ("pair",))
)
LABEL_GENERATION_SECONDS = REGISTRY.register(
    Histogram(
        "aiquant_label_generation_seconds",
        "单轮标签生成耗时（秒）",
        buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
    )
)
LABEL_HEARTBEAT_AGE = REGISTRY.register(
    Gauge("aiquant_label_heartbeat_age_seconds", "距上一次心跳写入的秒数")
)
READONLY_CHECK_OK = REGISTRY.register(
    Gauge("aiquant_readonly_check_ok", "只读 MCP 自检结果（1=通过 0=失败）")
)

# 执行域
GATE_TOTAL = REGISTRY.register(
    Counter(
        "aiquant_gate_total",
        "闸门裁决次数（allow/missing/block/reduce/error/disabled）",
        ("result",),
    )
)
GATE_ENABLED_GAUGE = REGISTRY.register(
    Gauge("aiquant_gate_enabled", "闸门开关状态（1=启用 0=关闭）")
)
GATE_DECISION_SECONDS = REGISTRY.register(
    Histogram(
        "aiquant_gate_decision_seconds",
        "confirm_trade_entry 裁决耗时（秒），要求 P99 < 5ms",
        buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.05, 0.5),
    )
)
CLI_CALLS = REGISTRY.register(
    Counter("aiquant_cli_calls_total", "okx-trade-cli 调用次数（success/failure/timeout）", ("result",))
)
CLI_LATENCY = REGISTRY.register(
    Histogram(
        "aiquant_cli_seconds",
        "okx-trade-cli 调用耗时（秒）",
        ("action",),
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20),
    )
)
FREQTRADE_PAUSED = REGISTRY.register(
    Gauge("aiquant_freqtrade_paused", "Freqtrade 暂停状态（1=暂停 0=运行）")
)
SYSTEM_HALT = REGISTRY.register(Gauge("aiquant_system_halt", "system:halt 标志（1=halt）"))
GUARDER_CHECK_SECONDS = REGISTRY.register(
    Histogram("aiquant_guarder_check_seconds", "守门服务健康检查耗时（秒）")
)


def render_metrics() -> str:
    return REGISTRY.render()
