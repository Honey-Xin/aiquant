"""集中式配置：全部来自环境变量 / .env，代码中不出现任何密钥字面量。"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ---------------- OKX 凭证（仅执行域） ----------------
    okx_api_key: str = Field(default="", validation_alias=AliasChoices("OKX_API_KEY", "OKX_KEY"))
    okx_api_secret: str = Field(
        default="", validation_alias=AliasChoices("OKX_API_SECRET", "OKX_SECRET")
    )
    okx_api_passphrase: str = Field(
        default="", validation_alias=AliasChoices("OKX_API_PASSPHRASE", "OKX_PASSPHRASE")
    )
    okx_demo_trading: bool = True

    # ---------------- OKX 只读 MCP / 交易 CLI ----------------
    okx_mcp_readonly_cmd: str = "okx-trade-mcp --read-only"
    okx_cli_cmd: str = "okx-trade-cli"
    cli_timeout_sec: float = 10.0
    readonly_check_required: bool = True

    # ---------------- Redis ----------------
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---------------- 标签服务 ----------------
    label_pairs: str = "BTC/USDT,ETH/USDT,SOL/USDT"
    label_ttl_sec: int = 300
    label_cycle_sec: int = 60
    label_heartbeat_sec: int = 30
    label_heartbeat_ttl_sec: int = 60
    label_schema_retry: int = 1
    label_host: str = "0.0.0.0"
    label_port: int = 8081

    # ---------------- LLM ----------------
    llm_provider: Literal["openai", "anthropic", "deepseek"] = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_timeout_sec: float = 30.0
    llm_max_tokens: int = 900
    llm_temperature: float = 0.0

    # ---------------- 新闻源 ----------------
    news_enabled: bool = False
    news_provider: str = "cryptopanic"
    news_api_url: str = "https://cryptopanic.com/api/v1/posts/"
    news_api_token: str = ""
    news_timeout_sec: float = 10.0
    news_max_items: int = 20

    # ---------------- 闸门 ----------------
    gate_enabled: bool = True
    gate_fail_open: bool = True
    gate_min_amount_json: str = '{"BTC/USDT":0.0001,"ETH/USDT":0.001,"SOL/USDT":0.01}'
    gate_host: str = "0.0.0.0"
    gate_port: int = 8080
    gate_api_token: str = ""
    label_service_url: str = "http://127.0.0.1:8081"
    freqtrade_api_url: str = "http://127.0.0.1:8082"
    freqtrade_api_user: str = "freqtrader"
    freqtrade_api_password: str = ""

    # ---------------- 验证（P2） ----------------
    wfa_embargo_bars: int = 24
    wfa_train_bars: int = 2016
    wfa_test_bars: int = 504
    wfa_step_bars: int = 504
    approve_sharpe_delta: float = 0.1
    approve_mdd_reduction: float = 0.10
    approve_max_trade_drop: float = 0.30
    approve_catastrophe_mult: float = 1.5

    # ---------------- 审计 ----------------
    audit_log_dir: str = "./logs"
    audit_retention_days: int = 180

    # ---------------- 日志 ----------------
    log_level: str = "INFO"

    # ---------------- 派生值 ----------------
    @field_validator("gate_fail_open")
    @classmethod
    def _fail_open_must_be_true(cls, v: bool) -> bool:
        """FR/§6.2：GATE_FAIL_OPEN 必须为 true，否则标签异常会阻塞交易。"""
        if v is not True:
            raise ValueError("GATE_FAIL_OPEN 必须为 true：标签缺失/异常一律放行")
        return v

    @model_validator(mode="after")
    def _check_pairs(self) -> "Settings":
        if not self.pairs:
            raise ValueError("LABEL_PAIRS 不能为空")
        return self

    @property
    def pairs(self) -> list[str]:
        """交易对统一大写去重，保证与标签 key（``ai_gate:{pair}``）一致。"""
        seen: list[str] = []
        for raw in self.label_pairs.split(","):
            pair = raw.strip().upper()
            if pair and pair not in seen:
                seen.append(pair)
        return seen

    @property
    def min_amounts(self) -> dict[str, float]:
        try:
            raw: Any = json.loads(self.gate_min_amount_json or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k).strip().upper()] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def secrets(self) -> list[str]:
        """用于日志脱敏的敏感值清单（只在本进程内存中使用）。"""
        return [
            s
            for s in (
                self.okx_api_key,
                self.okx_api_secret,
                self.okx_api_passphrase,
                self.llm_api_key,
                self.news_api_token,
                self.gate_api_token,
                self.freqtrade_api_password,
            )
            if s and len(s) >= 4
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：环境变量改动后重新读取。"""
    get_settings.cache_clear()
