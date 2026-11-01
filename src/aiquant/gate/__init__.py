"""闸门域：确定性裁决、守门可用性、HTTP 服务。

包级导入一律懒加载（PEP 562）：Freqtrade 策略只需要 ``decision``，
不得因为导入本包而把 httpx / guarder 拉进实盘进程（FR-2.1）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "Gate",
    "GateDecision",
    "GateToggle",
    "FreqtradeClient",
    "Guarder",
    "make_guarder",
    "HALT_KEY",
    "RESULT_ALLOW",
    "RESULT_BLOCK",
    "RESULT_ERROR",
    "RESULT_MISSING",
    "RESULT_REDUCE",
]

_LAZY = {
    "Gate": ("decision", "Gate"),
    "GateDecision": ("decision", "GateDecision"),
    "GateToggle": ("decision", "GateToggle"),
    "HALT_KEY": ("decision", "HALT_KEY"),
    "RESULT_ALLOW": ("decision", "RESULT_ALLOW"),
    "RESULT_BLOCK": ("decision", "RESULT_BLOCK"),
    "RESULT_ERROR": ("decision", "RESULT_ERROR"),
    "RESULT_MISSING": ("decision", "RESULT_MISSING"),
    "RESULT_REDUCE": ("decision", "RESULT_REDUCE"),
    "FreqtradeClient": ("freqtrade_client", "FreqtradeClient"),
    "Guarder": ("guarder", "Guarder"),
    "make_guarder": ("guarder", "make_guarder"),
}


def __getattr__(name: str) -> Any:  # pragma: no cover - 简单转发
    try:
        module_name, attr = _LAZY[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    from importlib import import_module

    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:  # pragma: no cover
    return sorted(__all__)


if TYPE_CHECKING:  # pragma: no cover
    from .decision import (
        HALT_KEY,
        RESULT_ALLOW,
        RESULT_BLOCK,
        RESULT_ERROR,
        RESULT_MISSING,
        RESULT_REDUCE,
        Gate,
        GateDecision,
        GateToggle,
    )
    from .freqtrade_client import FreqtradeClient
    from .guarder import Guarder, make_guarder
