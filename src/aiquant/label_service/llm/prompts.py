"""Prompt 构造。

硬性要求：
  * prompt 中只放"该时间点之前可得的行情/资讯"，不放任何密钥、账户、订单 ID。
  * 明确要求严格 JSON、枚举取值、以及 reduce_size 的乘数约束。
  * 模型被要求只做"否决/降仓"判断，不做涨跌预测（§1.2）。
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """你是加密货币市场的风险观察员。你不预测涨跌，也不建议方向。
你的唯一职责是：基于给定的只读市场数据与资讯，输出一个强类型的风险状态标签，供下游确定性风控程序裁决。

输出要求（违反任何一条都会被判为无效）：
1. 只输出一个 JSON 对象，不能有 markdown 代码块、不能有任何解释文字、不能有前后缀。
2. 必须包含字段：pair, generated_at, ttl_sec, regime, event_flags, action, size_multiplier, confidence, evidence。
3. regime 只能是以下之一：risk_on, risk_off, event_driven, illiquid, unknown。
4. event_flags 只能取：macro_event, token_unlock, exchange_incident, listing_delisting, regulatory, liquidity_stress, volatility_spike；可为空数组。
5. action 只能是：allow（正常放行）、reduce_size（降低仓位）、block_new_entries（禁止新开仓）。
6. action=reduce_size 时 size_multiplier 必须在 (0,1] 区间；action=allow 时 size_multiplier 必须为 1.0；action=block_new_entries 时 size_multiplier 被忽略（写 0 即可）。
7. confidence 只能是 low, medium, high；它只用于审计，不参与自动裁决。
8. evidence 为 0~10 条，每条包含 source, summary, ref；不得包含任何 API Key、Secret、Passphrase、token 或账户信息。
9. 数据不足或信号矛盾时，选择 action=allow、regime=unknown、confidence=low——宁可放行，不要臆断。
10. generated_at 使用当前时间的 ISO8601 UTC 格式（例如 2026-11-01T12:00:00Z）。
"""


def build_user_prompt(pair: str, snapshot: dict[str, Any], news: list[dict[str, Any]], now_iso: str, ttl_sec: int) -> str:
    payload = {
        "pair": pair,
        "now_utc": now_iso,
        "ttl_sec": ttl_sec,
        "market_snapshot": snapshot,
        "news": news,
        "output_template": {
            "pair": pair,
            "generated_at": now_iso,
            "ttl_sec": ttl_sec,
            "regime": "unknown",
            "event_flags": [],
            "action": "allow",
            "size_multiplier": 1.0,
            "confidence": "low",
            "evidence": [],
        },
    }
    return (
        "以下是只读市场数据与资讯（JSON）。请按系统指令输出该交易对的风险标签，"
        "只输出 JSON 对象本身：\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )


def build_retry_prompt(base_user_prompt: str, previous_output: str, error: str) -> str:
    """Schema 校验失败后的唯一一次重试：给出错误原因并要求纯 JSON。"""
    return (
        f"{base_user_prompt}\n\n"
        f"上一次输出未通过校验，错误：{error[:400]}\n"
        f"上一次输出片段：{previous_output[:400]}\n"
        "请只输出一个符合 Schema 的 JSON 对象，不要任何解释、代码块或前后缀。"
    )
