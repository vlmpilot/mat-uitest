"""grounding 评测脚本（`scripts/grounding_eval.py`）的契约测试。

它守三件事：
1. **命中判定是纯几何**——点在框内才算命中，容差是显式参数，不在别处偷偷放宽；
2. **未命中的两种形态分开计**：坐标在框外 vs 模型 `found=false`（"看错位置"与"看不见"
   是不同能力问题，混在一起就看不出该改 prompt 还是改模型）；
3. **调用失败不进分母**（与"skipped 不进分母"同一纪律），否则 API 抖动会被读成能力差。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "grounding_eval.py"
    spec = importlib.util.spec_from_file_location("grounding_eval", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOX = [72, 781, 1008, 949]


def test_hit_is_geometric_and_inclusive():
    ge = _load_module()
    assert ge.hit([539, 866], BOX, 0) is True
    assert ge.hit([72, 781], BOX, 0) is True, "左上角边界算命中"
    assert ge.hit([1008, 949], BOX, 0) is True, "右下角边界算命中"
    assert ge.hit([71, 866], BOX, 0) is False, "左侧越界 1px 就不算"
    assert ge.hit([539, 950], BOX, 0) is False, "下方越界 1px 就不算"
    assert ge.hit([1, 867], BOX, 0) is False, "真机实测的退化坐标（x 贴左边缘）"


def test_hit_requires_a_point():
    ge = _load_module()
    assert ge.hit(None, BOX, 0) is False, "模型没给坐标 = 不算命中"
    assert ge.hit([], BOX, 0) is False


def test_tolerance_is_opt_in_and_symmetric():
    """容差必须显式传，且两个方向都放宽同样的像素。"""
    ge = _load_module()
    assert ge.hit([71, 866], BOX, 0) is False
    assert ge.hit([71, 866], BOX, 8) is True
    assert ge.hit([1016, 866], BOX, 8) is True
    assert ge.hit([1017, 866], BOX, 8) is False


def test_wilson_bounds_behave_at_extremes():
    ge = _load_module()
    lo, hi = ge.wilson(0, 27)
    assert abs(lo) < 1e-9 and 0.10 < hi < 0.20, f"0/27 的上限应约 13%，实际 {hi:.3f}"
    lo, hi = ge.wilson(27, 27)
    assert hi == 1.0 and 0.80 < lo < 0.95
    assert ge.wilson(0, 0) == (0.0, 1.0), "n=0 不给假精度"


def test_summarize_excludes_errors_and_splits_not_found():
    ge = _load_module()

    def row(**kw):
        base = {"bucket": "middle", "kind": "input", "hit": False, "found": True, "error": None}
        base.update(kw)
        return base

    rows = [
        row(hit=True, found=True),
        row(hit=False, found=True),          # 看错位置
        row(hit=False, found=False),         # 看不见
        row(error="HTTPError: 500"),         # 调用失败：不进分母
    ]
    s = ge.summarize(rows)
    assert s["n"] == 3, "调用失败不进分母"
    assert (s["hits"], s["not_found"], s["errors"]) == (1, 1, 1)
    assert s["rate"] == round(1 / 3, 4)
    assert s["by_bucket"]["middle"]["n"] == 3
