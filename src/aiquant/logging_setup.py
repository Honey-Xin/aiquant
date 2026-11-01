"""统一日志：所有 handler 前面挂一个脱敏过滤器。

即使某处代码不小心把凭证拼进了日志（例如 stderr 摘要），落盘前也会被替换成
掩码——这是 G-11 的最后一道防线。
"""

from __future__ import annotations

import logging
import sys
from typing import Iterable

from aiquant.audit.logger import redact


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式错误的日志也必须能落地（脱敏后）
            message = str(record.msg)
        record.msg = redact(message, self._secrets)
        record.args = ()
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted, self._secrets)
            record.exc_info = None
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    handler.addFilter(RedactingFilter(secrets))

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
