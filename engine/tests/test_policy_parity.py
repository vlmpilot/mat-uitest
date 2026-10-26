"""P0-1：配额策略三处一致性（policy/ 唯一事实源 + 引擎副本 + 云副本；引擎/服务端解析一致）。

公开仓不含闭源侧 `cloud/`：云副本两断言在 cloud 缺席时优雅跳过（评审 P1-2），
引擎侧校验（policy 唯一事实源 ↔ 包内副本）在公开仓照常生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_packaged_copies_match_canonical():
    canonical = (REPO / "policy" / "quota.json").read_bytes()
    engine_copy = REPO / "engine" / "insta_engine" / "data" / "quota.json"
    assert engine_copy.is_file(), f"缺副本：{engine_copy}"
    assert engine_copy.read_bytes() == canonical, (
        f"副本与唯一事实源不一致：{engine_copy}（跑 scripts/sync_quota_policy.py 修复）")
    cloud_copy = REPO / "cloud" / "matcloud" / "data" / "quota.json"
    if not (REPO / "cloud").is_dir():
        pytest.skip("公开仓不含闭源侧 cloud/（引擎副本已在上行校验）")
    assert cloud_copy.read_bytes() == canonical, (
        f"副本与唯一事实源不一致：{cloud_copy}（跑 scripts/sync_quota_policy.py 修复）")


def test_engine_and_cloud_limits_agree():
    if not (REPO / "cloud").is_dir():
        pytest.skip("公开仓不含闭源侧 cloud/，服务端解析一致性在闭源仓 CI 校验")
    sys.path.insert(0, str(REPO / "cloud"))
    try:
        from matcloud import config as cloud_config  # noqa: E402

        from insta_engine.quota_policy import plan_limits  # noqa: E402

        for plan in ("free", "pro", "team", "private"):
            eng = plan_limits(plan)
            cld = cloud_config.PLAN_LIMITS.get(plan)
            assert cld is not None, f"服务端缺档位 {plan}"
            for key in ("gen", "vision"):
                ev, cv = eng[key], cld[key]
                if ev is None:
                    # 引擎口径 null=不限；服务端以极大数表达
                    assert cv >= 1e9, f"{plan}.{key}：引擎=不限 服务端={cv}"
                else:
                    assert abs(float(cv) - float(ev)) < 1e-9, (
                        f"{plan}.{key}：引擎={ev} 服务端={cv}（历史漂移，P0-1 要防的正是这个）")
    finally:
        sys.path.remove(str(REPO / "cloud"))
