"""内存存储实现：单测与本地 dev 用，不参与生产。"""

from __future__ import annotations

import threading
import time


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.RLock()
        self._available = True

    # ------------------------------------------------------------- 契约
    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expire_at = item
            if expire_at is not None and expire_at <= time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def setex(self, key: str, ttl_sec: int, value: str) -> None:
        with self._lock:
            expire_at = None if ttl_sec <= 0 else time.monotonic() + float(ttl_sec)
            self._data[key] = (value, expire_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def ping(self) -> bool:
        return self._available

    # ------------------------------------------------------------- 测试钩子
    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data)

    def set_available(self, available: bool) -> None:
        """模拟 Redis 宕机，用于验证标签异常时默认放行。"""
        self._available = available

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def ttl(self, key: str) -> float | None:
        with self._lock:
            item = self._data.get(key)
            if item is None or item[1] is None:
                return None
            return max(0.0, item[1] - time.monotonic())
