"""审计日志：JSONL 落盘、按时间范围导出、脱敏、保留期清理、每日摘要。

约束（§10.3 / G-11）：API Key、Secret、Passphrase 一律不得出现在日志、
错误堆栈、标签或 LLM prompt 中。本模块是所有落盘内容的唯一出口，因此脱敏
必须发生在这里，而不是靠调用方自觉。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# --------------------------------------------------------------------- 脱敏
_MASK = "***REDACTED***"

_SECRET_KEY_RE = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret[_-]?key|secret|passphrase|password|"
    r"token|jwt[_-]?secret|ws[_-]?token|authorization|access[_-]?key)\b"
    r"(\s*[:=]\s*|[\"']\s*:\s*[\"'])"
    r"([\"']?)([^\s\"',}]{4,})"
)

_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9\-_.=+/]{8,})")

# OKX / 通用 api key 形态：32~64 位十六进制、或 base64-ish 长串（用于兜底）
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,64}\b")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/_-]{40,88}={0,2}\b")

# 分片文件名尾部日期：{stream}-YYYY-MM-DD.jsonl
_SHARD_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})$")


def redact(text: Any, extra_secrets: Iterable[str] = ()) -> str:
    """把任意文本里的凭证替换为掩码。对非字符串先转 str。"""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)

    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            s = s.replace(secret, _MASK)

    s = _BEARER_RE.sub(lambda m: f"{m.group(1)} {_MASK}", s)
    s = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_MASK}", s)
    s = _LONG_HEX_RE.sub(_MASK, s)
    s = _LONG_B64_RE.sub(_MASK, s)
    return s


def looks_like_credential(text: str | None, extra_secrets: Iterable[str] = ()) -> bool:
    """判断文本是否像凭证。

    只匹配"键名 + 值"形态和 Bearer 令牌，不做通用的长串匹配——否则链上
    tx hash、memo 之类会被误判，标签 evidence 会被大面积误拒。
    """
    if not text:
        return False
    for secret in extra_secrets:
        if secret and len(secret) >= 8 and secret in text:
            return True
    if _BEARER_RE.search(text):
        return True
    return bool(_SECRET_KEY_RE.search(text))


def redact_obj(obj: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """递归脱敏 dict / list / str 结构，返回新对象。"""
    secrets = tuple(extra_secrets)
    if isinstance(obj, dict):
        return {redact(k, secrets): redact_obj(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, secrets) for v in obj]
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return redact(repr(obj), secrets)


# --------------------------------------------------------------- 文件写出入
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    txt = value.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


class AuditLog:
    """按 ``{stream}-YYYY-MM-DD.jsonl`` 分片写入的审计日志。"""

    def __init__(
        self,
        log_dir: str | Path | None = None,
        retention_days: int = 180,
        secrets: Iterable[str] = (),
    ) -> None:
        self.log_dir = Path(log_dir or os.environ.get("AUDIT_LOG_DIR", "./logs"))
        self.retention_days = int(retention_days)
        self._secrets = tuple(secrets)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 写
    def _path_for(self, stream: str, day: date) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stream)
        return self.log_dir / f"{safe}-{day.isoformat()}.jsonl"

    def write(self, stream: str, event: str, **fields: Any) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        now = _utcnow()
        record = {
            "ts": _iso(now),
            "stream": stream,
            "event": event,
            **fields,
        }
        payload = redact_obj(record, self._secrets)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        path = self._path_for(stream, now.date())
        with self._lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return path

    # ---------------------------------------------------------------- 读
    def iter_records(
        self,
        stream: str | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> Iterator[dict[str, Any]]:
        start_ts = _parse_iso(start) if isinstance(start, str) else start
        end_ts = _parse_iso(end) if isinstance(end, str) else end

        if not self.log_dir.exists():
            return
        for path in sorted(self.log_dir.glob("*.jsonl")):
            if stream and not path.name.startswith(f"{stream}-"):
                continue
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_iso(str(rec.get("ts", "")))
                    if ts is None:
                        continue
                    if start_ts and ts < start_ts:
                        continue
                    if end_ts and ts > end_ts:
                        continue
                    yield rec

    def export(
        self,
        out_path: str | Path,
        stream: str | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> Path:
        """按时间范围导出（复盘用）。输出 JSONL，内容已脱敏。"""
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with out.open("w", encoding="utf-8") as fh:
            for rec in self.iter_records(stream=stream, start=start, end=end):
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        return out

    # ---------------------------------------------------------------- 清理
    def purge_old(self, now: datetime | None = None) -> int:
        """删除超过保留期的分片文件，返回删除数量。"""
        if not self.log_dir.exists():
            return 0
        cutoff = (now or _utcnow()).date() - timedelta(days=self.retention_days)
        removed = 0
        for path in self.log_dir.glob("*.jsonl"):
            m = _SHARD_DATE_RE.search(path.stem)
            if not m:
                continue
            try:
                day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed


# --------------------------------------------------------------- 每日摘要
@dataclass
class DailySummary:
    day: str
    label_count: int = 0
    label_actions: dict[str, int] = field(default_factory=dict)
    gate_results: dict[str, int] = field(default_factory=dict)
    order_count: int = 0
    error_count: int = 0
    cli_failures: int = 0
    readonly_check: str | None = None
    halt_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "label_count": self.label_count,
            "label_actions": self.label_actions,
            "gate_results": self.gate_results,
            "order_count": self.order_count,
            "error_count": self.error_count,
            "cli_failures": self.cli_failures,
            "readonly_check": self.readonly_check,
            "halt_events": self.halt_events,
        }

    def render_markdown(self) -> str:
        lines = [f"# 审计摘要 {self.day}", ""]
        lines.append(f"- 标签数量: **{self.label_count}**")
        if self.label_actions:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.label_actions.items()))
            lines.append(f"- 标签 action 分布: {detail}")
        if self.gate_results:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.gate_results.items()))
            lines.append(f"- gate_* 分布: {detail}")
        lines.append(f"- 订单数量: **{self.order_count}**")
        lines.append(f"- 异常次数: **{self.error_count}**")
        lines.append(f"- CLI 失败次数: **{self.cli_failures}**")
        lines.append(f"- 只读自检: {self.readonly_check or 'n/a'}")
        lines.append(f"- halt 事件: {self.halt_events}")
        return "\n".join(lines) + "\n"


def daily_summary(log_dir: str | Path, day: date | str | None = None) -> DailySummary:
    """从审计日志聚合出每日摘要（§12.3）。"""
    audit = AuditLog(log_dir=log_dir)
    if day is None:
        day_date = _utcnow().date()
    elif isinstance(day, str):
        day_date = date.fromisoformat(day)
    else:
        day_date = day
    start = datetime(day_date.year, day_date.month, day_date.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    summary = DailySummary(day=day_date.isoformat())
    for rec in audit.iter_records(start=start, end=end):
        event = rec.get("event", "")
        stream = rec.get("stream", "")
        if event == "label_written":
            summary.label_count += 1
            action = str(rec.get("action", "unknown"))
            summary.label_actions[action] = summary.label_actions.get(action, 0) + 1
        elif event == "gate_decision":
            result = str(rec.get("result", "unknown"))
            summary.gate_results[result] = summary.gate_results.get(result, 0) + 1
        elif event == "order_submitted":
            summary.order_count += 1
        elif event in {"error", "exception"} or stream == "error":
            summary.error_count += 1
        elif event == "cli_failed":
            summary.cli_failures += 1
        elif event == "readonly_check":
            summary.readonly_check = str(rec.get("status", "unknown"))
        elif event == "halt_detected":
            summary.halt_events += 1
    return summary
