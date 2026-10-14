"""守卫：engine/LICENSE 必须与仓库根 LICENSE 逐字节一致（防漂移，2026-10-10）。

为什么需要这条断言
------------------
`pyproject.toml` 在 `engine/` 下，PEP 639 的 `license-files = ["LICENSE"]` 是**相对项目根**
解析的，构建根引用不到父目录的 `../LICENSE` ⇒ `engine/LICENSE` 只能是根 `LICENSE` 的**副本**。

副本会漂移：有人改了根 `LICENSE`（换年份、加署名、改链接）却忘了同步副本。后果是
「`pip install` 装到的 wheel 里的许可证文本 ≠ GitHub 上看到的 LICENSE」——这是**法务级**
不一致，而且**不会让任何功能测试变红**（它不发红包，只发错文本）。故用一条断言把
"两份必须相同"从口头约定变成机器校验。

覆盖范围：仓库同时含根 `LICENSE` 与 `engine/LICENSE`，本测试保证两者逐字一致；
CI（`pytest engine/tests`）与本地全量门禁都会跑到它。

反向验证（确认这条测试真的能抓到漂移，而不是恒真）：
    printf '\n' >> engine/LICENSE && pytest engine/tests/test_license_sync.py -q  # 应红
    git checkout -- engine/LICENSE                                               # 复原
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_LICENSE = REPO_ROOT / "LICENSE"
ENGINE_LICENSE = REPO_ROOT / "engine" / "LICENSE"


def test_engine_license_exists():
    """engine/LICENSE 必须存在——缺它会让 wheel 里没有许可证全文（PEP 639 静默降级）。"""
    assert ENGINE_LICENSE.exists(), (
        "engine/LICENSE 缺失：pyproject 的 license-files=['LICENSE'] 以 engine/ 为构建根，"
        "缺它则 wheel 不含许可证全文。修法：cp LICENSE engine/LICENSE"
    )


def test_engine_license_is_byte_identical_to_root():
    """engine/LICENSE 与根 LICENSE 必须逐字节相同（漂移即失败）。"""
    if not ROOT_LICENSE.exists():
        pytest.skip("仓库根 LICENSE 不存在（例如已安装为包、无源码树）")
    assert ENGINE_LICENSE.read_bytes() == ROOT_LICENSE.read_bytes(), (
        "engine/LICENSE 与根 LICENSE 不一致（漂移）——"
        "这会让 pip 装到的许可证文本 ≠ GitHub 上的 LICENSE。修法：cp LICENSE engine/LICENSE"
    )
