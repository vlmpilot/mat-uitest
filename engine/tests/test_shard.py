"""用例分片（`--shard K/N`）的契约测试。

分片要能真并行，两个性质不能破：
1. **划分稳定**：不同 runner 拿到的是同一套划分（按文件名排序，不看输入顺序）；
2. **不重不漏**：各分片并起来恰好等于全集。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_engine.__main__ import _shard_files


def _files(n: int) -> list[Path]:
    return [Path(f"cases/case-{i:02d}.json") for i in range(n)]


def test_shards_are_disjoint_and_cover_all():
    files = _files(7)
    parts = [_shard_files(files, f"{k}/3") for k in (1, 2, 3)]
    union = [p for part in parts for p in part]
    assert sorted(union) == sorted(files), "各分片并起来必须等于全集（不重不漏）"
    assert len(union) == len(set(union)), "同一条用例不能出现在两个分片"
    assert [len(p) for p in parts] == [3, 2, 2], "7 条切 3 份应为 3/2/2"


def test_shard_order_is_stable_regardless_of_input_order():
    """输入顺序变（glob 顺序在不同机器上可能不同），划分必须不变。"""
    files = _files(6)
    reversed_input = list(reversed(files))
    for k in (1, 2, 3):
        assert _shard_files(files, f"{k}/3") == _shard_files(reversed_input, f"{k}/3")


def test_single_shard_runs_everything():
    files = _files(4)
    assert _shard_files(files, "1/1") == sorted(files)


def test_last_shard_is_included():
    """K=N 必须有效（CI 矩阵里最后一格最容易被 off-by-one 吃掉）。"""
    files = _files(5)
    assert _shard_files(files, "5/5") == [Path("cases/case-04.json")]


def test_shard_more_than_files_yields_empty_slice():
    files = _files(2)
    assert _shard_files(files, "3/5") == [], "分片多于用例时该片为空（调用方按成功处理）"


@pytest.mark.parametrize("bad", ["0/3", "4/3", "1/0", "abc", "1-3", ""])
def test_invalid_shard_specs_raise(bad):
    with pytest.raises(ValueError):
        _shard_files(_files(3), bad)
