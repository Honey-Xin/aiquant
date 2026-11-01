"""订单执行器：所有下单动作经 ``okx-trade-cli``，由 Freqtrade 触发。

设计约束（需求 §5.1）：
  * AI / LLM / 标签服务不直接下单，也不直接调用本模块；
  * 本模块只被 Freqtrade 策略或运维手工调用；
  * 有超时、有限重试、失败即审计，绝不无限重试。

CLI 参数形态以 ``okx-trade-cli`` 为准（版本见 README）。命令模板可被覆盖，
便于适配不同版本的 flag 命名。
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from aiquant import metrics as M
from aiquant.audit.logger import AuditLog, redact_obj
from aiquant.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_TEMPLATE = (
    "{cli} --json {action} --inst-id {pair} --side {side} --size {amount}"
    "{price_part}{extra}"
)

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - 命令由本模块构造，参数已分片
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass
class ExecResult:
    ok: bool
    action: str
    pair: str
    side: str
    amount: float
    price: float | None = None
    order_id: str | None = None
    code: int = 0
    attempts: int = 0
    latency_ms: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    cmd: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "pair": self.pair,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "order_id": self.order_id,
            "code": self.code,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 3),
            "error": self.error,
        }


class OkxCliExecutor:
    def __init__(
        self,
        cli: str = "okx-trade-cli",
        timeout_sec: float = 10.0,
        retries: int = 1,
        template: str = DEFAULT_TEMPLATE,
        audit: AuditLog | None = None,
        runner: Runner | None = None,
        extra_args: Sequence[str] = (),
    ) -> None:
        self.cli = cli
        self.timeout_sec = timeout_sec
        self.retries = max(0, retries)
        self.template = template
        self.audit = audit or AuditLog()
        self.runner = runner or _default_runner
        self.extra_args = list(extra_args)

    # ------------------------------------------------------------- 命令
    def build_command(
        self,
        action: str,
        pair: str,
        side: str,
        amount: float,
        price: float | None = None,
    ) -> list[str]:
        price_part = f" --px {price}" if price is not None else ""
        extra = ("" if not self.extra_args else " " + " ".join(shlex.quote(a) for a in self.extra_args))
        line = self.template.format(
            cli=shlex.quote(self.cli),
            action=action,
            pair=pair,
            side=side,
            amount=amount,
            price_part=price_part,
            extra=extra,
        )
        return shlex.split(line)

    # ------------------------------------------------------------- 执行
    def execute(
        self,
        action: str,
        pair: str,
        side: str,
        amount: float,
        price: float | None = None,
    ) -> ExecResult:
        cmd = self.build_command(action, pair, side, amount, price)
        started = time.perf_counter()
        attempts = 0
        last: ExecResult | None = None

        for attempt in range(self.retries + 1):
            attempts = attempt + 1
            try:
                proc = self.runner(cmd, self.timeout_sec)
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                ok = proc.returncode == 0
                result = ExecResult(
                    ok=ok,
                    action=action,
                    pair=pair,
                    side=side,
                    amount=amount,
                    price=price,
                    code=proc.returncode,
                    attempts=attempts,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    stdout=stdout[:2000],
                    stderr=stderr[:2000],
                    error=None if ok else f"exit {proc.returncode}",
                    cmd=cmd,
                )
            except subprocess.TimeoutExpired:
                result = ExecResult(
                    ok=False,
                    action=action,
                    pair=pair,
                    side=side,
                    amount=amount,
                    price=price,
                    attempts=attempts,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error="timeout",
                    cmd=cmd,
                )
            except OSError as exc:
                result = ExecResult(
                    ok=False,
                    action=action,
                    pair=pair,
                    side=side,
                    amount=amount,
                    price=price,
                    attempts=attempts,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=f"oserror: {exc.__class__.__name__}",
                    cmd=cmd,
                )

            last = result
            if result.ok:
                break
            if result.error != "timeout":
                # 非超时错误（如参数错误、余额不足）重试无意义
                if attempt < self.retries and result.code not in {0} and result.code >= 500:
                    time.sleep(min(0.5 * (attempt + 1), 2.0))
                    continue
                break
            if attempt < self.retries:
                time.sleep(min(0.5 * (attempt + 1), 2.0))

        assert last is not None
        outcome = "ok" if last.ok else ("timeout" if last.error == "timeout" else "fail")
        M.CLI_CALLS.inc(result=outcome)
        M.CLI_LATENCY.observe(last.latency_ms / 1000.0, action=action)
        self.audit.write(
            "execution",
            "cli_call" if last.ok else "cli_error",
            **redact_obj(last.to_dict()),
        )
        if not last.ok:
            log.error("okx-trade-cli 失败 action=%s pair=%s error=%s", action, pair, last.error)
        return last

    # ------------------------------------------------------------- 便捷方法
    def market_buy(self, pair: str, amount: float) -> ExecResult:
        return self.execute("market-buy", pair, "buy", amount)

    def market_sell(self, pair: str, amount: float) -> ExecResult:
        return self.execute("market-sell", pair, "sell", amount)

    def limit_buy(self, pair: str, amount: float, price: float) -> ExecResult:
        return self.execute("limit-buy", pair, "buy", amount, price)

    def limit_sell(self, pair: str, amount: float, price: float) -> ExecResult:
        return self.execute("limit-sell", pair, "sell", amount, price)
