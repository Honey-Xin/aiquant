"""标签存储抽象：执行域只读、标签域只写。

执行域（gate）不得因为 Redis 故障而抛异常——所有方法必须返回可降级结果，
最终由闸门按 fail-open 处理。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LabelStore(Protocol):
    """最小 key-value 契约，TTL 以秒为单位。"""

    def get(self, key: str) -> str | None: ...

    def setex(self, key: str, ttl_sec: int, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def ping(self) -> bool: ...


class StoreError(RuntimeError):
    """存储层错误。闸门内部捕获后按 fail-open 处理。"""
