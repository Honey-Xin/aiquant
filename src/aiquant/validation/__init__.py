"""验证域：标签回放、Purged Walk-Forward、A/B 报告。

⚠️ 本包**不得**被实盘路径（``gate`` / ``execution`` / ``label_service``）导入。
``tests/test_architecture.py`` 会用 AST 检查这一点，防止验证代码混进交易链路。
"""

from .replay import Bar, LabelHistory, LabelRecord, ReplayEngine, ReplayResult, ReplayTrade
from .report import ABReport, Metrics, compare, compute_metrics, decide
from .wfa import Fold, PurgedWalkForward, assert_no_overlap

__all__ = [
    "Bar",
    "LabelHistory",
    "LabelRecord",
    "ReplayEngine",
    "ReplayResult",
    "ReplayTrade",
    "ABReport",
    "Metrics",
    "compare",
    "compute_metrics",
    "decide",
    "Fold",
    "PurgedWalkForward",
    "assert_no_overlap",
]
