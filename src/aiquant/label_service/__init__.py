"""AI 标签服务子包。"""

from .schema import Label, SchemaError, fallback_allow_label, parse_label, parse_with_retry

__all__ = ["Label", "SchemaError", "fallback_allow_label", "parse_label", "parse_with_retry"]
