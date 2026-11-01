"""标签存储实现。"""

from __future__ import annotations

import os

from .base import LabelStore, StoreError
from .memory_store import MemoryStore
from .redis_store import RedisStore

__all__ = ["LabelStore", "StoreError", "MemoryStore", "RedisStore", "build_store"]


def build_store(url: str | None = None, prefer_memory: bool = False) -> LabelStore:
    """工厂：默认 Redis；``AIQUANT_STORE=memory`` 或 prefer_memory 时用内存。"""
    if prefer_memory or os.environ.get("AIQUANT_STORE", "").lower() == "memory":
        return MemoryStore()
    return RedisStore(url or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
