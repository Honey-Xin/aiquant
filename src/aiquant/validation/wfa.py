"""Purged Walk-Forward 切分（FR-3.1）。

* 顺序切分，绝不 shuffle；
* 训练段与测试段之间留 ``embargo`` 根 bar 的空隙，避免标签窗口与测试段重叠
  （标签 TTL 默认 300s，即 5 根 1m bar 或 1 根 5m bar，embargo 默认 24 更保守）；
* 产出可直接喂给回放的测试段索引。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: int
    train_end: int  # 开区间
    test_start: int
    test_end: int  # 开区间
    embargo: int

    @property
    def train_size(self) -> int:
        return max(0, self.train_end - self.train_start)

    @property
    def test_size(self) -> int:
        return max(0, self.test_end - self.test_start)


class PurgedWalkForward:
    def __init__(
        self,
        n_splits: int = 4,
        embargo: int = 24,
        min_train_size: int | None = None,
        expanding: bool = True,
    ) -> None:
        if n_splits < 1:
            raise ValueError("n_splits 必须 >= 1")
        if embargo < 0:
            raise ValueError("embargo 必须 >= 0")
        self.n_splits = n_splits
        self.embargo = embargo
        self.min_train_size = min_train_size
        self.expanding = expanding

    def split(self, n_samples: int) -> Iterator[Fold]:
        if n_samples <= 0:
            return
        test_size = n_samples // (self.n_splits + 1)
        if test_size <= 0:
            return
        if self.min_train_size is None:
            train_size = test_size
        else:
            train_size = max(test_size, self.min_train_size)

        for k in range(self.n_splits):
            test_start = train_size + k * test_size + (0 if self.expanding else self.embargo)
            test_end = min(test_start + test_size, n_samples)
            if test_start >= n_samples or test_end - test_start <= 0:
                break
            test_start = min(test_start, n_samples)

            if self.expanding:
                train_start = 0
                train_end = max(0, test_start - self.embargo)
            else:
                train_start = max(0, test_start - self.embargo - train_size)
                train_end = max(0, test_start - self.embargo)

            if train_end <= train_start:
                continue
            yield Fold(
                index=k,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                embargo=self.embargo,
            )

    def split_list(self, n_samples: int) -> list[Fold]:
        return list(self.split(n_samples))


def assert_no_overlap(folds: Sequence[Fold], n_samples: int) -> None:
    """自检：测试段互不重叠、且都在样本范围内、训练段严格早于测试段。"""
    prev_end = -1
    for fold in folds:
        assert 0 <= fold.train_start < fold.train_end <= n_samples, f"fold {fold.index} 训练段越界"
        assert 0 <= fold.test_start < fold.test_end <= n_samples, f"fold {fold.index} 测试段越界"
        assert fold.train_end <= fold.test_start, f"fold {fold.index} 训练段侵入测试段"
        assert fold.test_start >= prev_end, f"fold {fold.index} 测试段重叠"
        prev_end = fold.test_end
