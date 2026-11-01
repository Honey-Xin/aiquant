"""Redis 实现：标签域写入、执行域低延迟读取（P99 目标 < 5ms）。"""

from __future__ import annotations

from typing import Any

from .base import StoreError


class RedisStore:
    """薄封装，连接不上时抛 StoreError；调用方必须自行降级。

    ``socket_timeout`` 默认 50ms：闸门在 confirm_trade_entry 中读一次标签，
    任何网络抖动都必须在毫秒级失败，不能让下单路径挂住。
    """

    def __init__(
        self,
        url: str,
        socket_timeout: float = 0.05,
        socket_connect_timeout: float = 0.05,
        client: Any | None = None,
    ) -> None:
        self.url = url
        self._client = client
        self._socket_timeout = socket_timeout
        self._socket_connect_timeout = socket_connect_timeout

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import redis  # 延迟导入：执行域未安装 redis 包时也能 import 本模块
            except ImportError as exc:  # pragma: no cover - 依赖缺失路径
                raise StoreError(f"redis 客户端不可用: {exc}") from exc
            self._client = redis.Redis.from_url(
                self.url,
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_connect_timeout,
                health_check_interval=30,
                decode_responses=True,
            )
        return self._client

    # ------------------------------------------------------------- 契约
    def get(self, key: str) -> str | None:
        try:
            value = self.client.get(key)
        except StoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成降级信号
            raise StoreError(f"redis get 失败: {exc}") from exc
        if value is None:
            return None
        return value if isinstance(value, str) else value.decode("utf-8")

    def setex(self, key: str, ttl_sec: int, value: str) -> None:
        try:
            self.client.setex(key, max(1, int(ttl_sec)), value)
        except StoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError(f"redis setex 失败: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete(key)
        except StoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError(f"redis delete 失败: {exc}") from exc

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:  # noqa: BLE001 - 健康检查必须永不抛出
            return False
