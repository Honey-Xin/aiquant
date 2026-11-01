"""标签 Schema（FR-1.3）。

LLM 必须输出严格 JSON（不得有自然语言前后缀），并且 100% 通过本 Schema。
校验失败：重试 1 次；仍失败则写入 ``action=allow`` 的兜底标签并记录错误。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aiquant.audit.logger import looks_like_credential

Regime = Literal["risk_on", "risk_off", "event_driven", "illiquid", "unknown"]
Action = Literal["allow", "reduce_size", "block_new_entries"]
Confidence = Literal["low", "medium", "high"]

KNOWN_EVENT_FLAGS = (
    "macro_event",
    "token_unlock",
    "exchange_incident",
    "listing_delisting",
    "regulatory",
    "liquidity_stress",
    "volatility_spike",
)

MAX_EVIDENCE = 10


class SchemaError(ValueError):
    """标签不合法。调用方必须按 fail-open 处理。"""


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=280)
    ref: str | None = Field(default=None, max_length=280)

    @field_validator("source", "summary", "ref")
    @classmethod
    def _no_credentials(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if looks_like_credential(v):
            raise ValueError("evidence 不得包含密钥或敏感凭证")
        return v


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair: str = Field(min_length=3, max_length=32)
    generated_at: datetime
    ttl_sec: int = Field(default=300, ge=1, le=86_400)
    regime: Regime
    event_flags: list[str] = Field(default_factory=list, max_length=8)
    action: Action
    size_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)
    confidence: Confidence = "low"
    evidence: list[Evidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)

    # ------------------------------------------------------------- 校验
    @field_validator("pair")
    @classmethod
    def _pair_format(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError("pair 必须形如 BTC/USDT")
        base, _, quote = v.partition("/")
        if not base or not quote:
            raise ValueError("pair 必须形如 BTC/USDT")
        return f"{base.strip().upper()}/{quote.strip().upper()}"

    @field_validator("generated_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("generated_at 必须带时区（ISO8601，推荐 Z）")
        return v.astimezone(timezone.utc)

    @field_validator("event_flags")
    @classmethod
    def _flags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for flag in v:
            f = str(flag).strip().lower()
            if not f:
                continue
            if f not in KNOWN_EVENT_FLAGS:
                raise ValueError(f"未知 event_flag: {flag}")
            if f not in out:
                out.append(f)
        return out

    @model_validator(mode="after")
    def _action_consistency(self) -> "Label":
        if self.action == "reduce_size":
            if not (0.0 < self.size_multiplier <= 1.0):
                raise ValueError("action=reduce_size 时 size_multiplier 必须在 (0,1]")
        elif self.action == "allow":
            # allow 语义是满仓放行：乘数必须为 1.0，避免"放行但减仓"的歧义
            if self.size_multiplier != 1.0:
                raise ValueError("action=allow 时 size_multiplier 必须为 1.0")
        # action=block_new_entries 时 size_multiplier 被忽略，不做约束
        return self

    # ------------------------------------------------------------- 派生
    def expiry(self) -> datetime:
        return self.generated_at + timedelta(seconds=self.ttl_sec)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expiry() < (now or datetime.now(timezone.utc))

    def to_json(self) -> str:
        payload = self.model_dump(mode="json")
        payload["generated_at"] = (
            self.generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Label":
        return parse_label(raw)


def _strict_json(raw: str) -> dict[str, Any]:
    """严格 JSON：不接受 markdown 代码块、前后缀自然语言。"""
    if not isinstance(raw, str):
        raise SchemaError("LLM 输出必须是字符串")
    text = raw.strip()
    if text.startswith("```") or text.endswith("```"):
        raise SchemaError("LLM 输出包含 markdown 代码块，违反严格 JSON 要求")
    if not (text.startswith("{") and text.endswith("}")):
        raise SchemaError("LLM 输出必须是一个 JSON 对象，不得包含任何前后缀")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("顶层必须是 JSON 对象")
    return data


def parse_label(raw: str) -> Label:
    """严格解析 + Schema 校验。失败抛 SchemaError（调用方负责重试/fail-open）。"""
    data = _strict_json(raw)
    try:
        return Label.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 及自定义 ValueError
        raise SchemaError(f"Schema 校验失败: {exc}") from exc


def fallback_allow_label(
    pair: str,
    reason: str,
    ttl_sec: int = 300,
    now: datetime | None = None,
) -> Label:
    """兜底标签：action=allow。标签服务任何异常都走这里。"""
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return Label(
        pair=pair,
        generated_at=ts,
        ttl_sec=max(1, int(ttl_sec)),
        regime="unknown",
        event_flags=[],
        action="allow",
        size_multiplier=1.0,
        confidence="low",
        evidence=[
            Evidence(
                source="label_service",
                summary=f"fallback allow: {reason}"[:280],
                ref="fallback",
            )
        ],
    )


def parse_with_retry(
    generator,
    pair: str,
    ttl_sec: int = 300,
    retries: int = 1,
    on_retry=None,
) -> tuple[Label, int]:
    """调用 generator() 生成 JSON 文本；校验失败重试 ``retries`` 次。

    返回 (label, attempts_used)。全部失败时返回兜底 allow 标签，attempts_used
    表示已消耗的次数，方便指标与审计（G-12：首次非法、第二次合法 → 写入合法标签）。
    """
    attempts = 0
    last_error: str | None = None
    for attempt in range(retries + 1):
        attempts = attempt + 1
        try:
            raw = generator()
        except Exception as exc:  # noqa: BLE001 - LLM/网络异常同样走兜底
            last_error = f"generator error: {exc}"
            if attempt < retries:
                if on_retry:
                    on_retry(attempt + 1, last_error)
                continue
            break
        try:
            label = parse_label(raw)
        except SchemaError as exc:
            last_error = str(exc)
            if attempt < retries:
                if on_retry:
                    on_retry(attempt + 1, last_error)
                continue
            break
        if label.pair != pair:
            # 交易对不匹配说明模型跑偏，按校验失败处理
            last_error = f"pair 不匹配: 期望 {pair}, 实际 {label.pair}"
            if attempt < retries:
                if on_retry:
                    on_retry(attempt + 1, last_error)
                continue
            break
        return label, attempts

    return fallback_allow_label(pair, last_error or "unknown", ttl_sec=ttl_sec), attempts
