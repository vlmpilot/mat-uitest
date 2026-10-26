"""state_home 用户目录迁移直测（MAT 评估 2026-10-24 B2：此前零覆盖）。

`~/.insta → ~/.mat` 的整树迁移是**一次性、动用户数据**的逻辑：迁移丢文件 =
license/usage/credentials/history 全部"消失"。用假 HOME 钉死判据：
优先级、迁移完整性、旧目录保留（备份）、.mat 已在时不重迁。
"""

from __future__ import annotations

import json


def _fake_home(monkeypatch, home) -> None:
    """把 `Path.home()` 钉到临时目录——**跨平台**。

    POSIX 的 expanduser 认 HOME，Windows 的 ntpath 只认 USERPROFILE（`Path.home()` 在
    Windows 上完全不看 HOME）。只设 HOME 时，这三条迁移用例在 Windows CI 上拿到的仍是
    真实用户目录，全部断言失败（2026-10-25 实测 ci-windows run 40168943752）。
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_env_priority_mat_home_wins(monkeypatch, tmp_path):
    from insta_engine.state import state_home

    monkeypatch.setenv("MAT_HOME", str(tmp_path / "m"))
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "i"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert state_home() == tmp_path / "m"


def test_env_priority_insta_home_fallback(monkeypatch, tmp_path):
    from insta_engine.state import state_home

    monkeypatch.delenv("MAT_HOME", raising=False)
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "i"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert state_home() == tmp_path / "i"


def test_legacy_insta_tree_is_migrated_and_kept_as_backup(monkeypatch, tmp_path):
    """默认路径：~/.insta 存在且 ~/.mat 不存在 ⇒ 整树复制，旧目录保留。"""
    from insta_engine.state import state_home

    home = tmp_path / "h"
    legacy = home / ".insta"
    (legacy / "creds").mkdir(parents=True)
    (legacy / "license.json").write_text("{}", encoding="utf-8")
    (legacy / "creds" / "store.json").write_text("{}", encoding="utf-8")
    _fake_home(monkeypatch, home)
    monkeypatch.delenv("MAT_HOME", raising=False)
    monkeypatch.delenv("INSTA_HOME", raising=False)

    got = state_home()
    assert got == home / ".mat", "迁移后应以 ~/.mat 为状态根"
    assert (home / ".mat" / "license.json").exists(), "顶层文件必须迁过去"
    assert (home / ".mat" / "creds" / "store.json").exists(), "子目录文件必须迁过去"
    assert (legacy / "license.json").exists(), "旧目录保留作备份（模块说明的承诺）"


def test_no_re_migration_when_mat_already_exists(monkeypatch, tmp_path):
    """~/.mat 已在 ⇒ 不再迁移（.insta 里后来新增的文件不应出现在 .mat）。"""
    from insta_engine.state import state_home

    home = tmp_path / "h"
    (home / ".mat").mkdir(parents=True)
    (home / ".insta").mkdir(parents=True)
    (home / ".insta" / "late.json").write_text("{}", encoding="utf-8")
    _fake_home(monkeypatch, home)
    monkeypatch.delenv("MAT_HOME", raising=False)
    monkeypatch.delenv("INSTA_HOME", raising=False)

    assert state_home() == home / ".mat"
    assert not (home / ".mat" / "late.json").exists(), "已迁移过就不该再复制"


def test_legacy_file_not_dir_is_ignored(monkeypatch, tmp_path):
    """.insta 是文件（不是目录）⇒ 不迁移、不报错，仍返回 ~/.mat。"""
    from insta_engine.state import state_home

    home = tmp_path / "h"
    home.mkdir(parents=True)
    (home / ".insta").write_text("not a dir", encoding="utf-8")
    _fake_home(monkeypatch, home)
    monkeypatch.delenv("MAT_HOME", raising=False)
    monkeypatch.delenv("INSTA_HOME", raising=False)

    assert state_home() == home / ".mat"


def test_ensure_default_workspace_is_idempotent_and_non_destructive(tmp_path):
    """配套判据：示例工作区铺设幂等，且**绝不覆盖**已存在文件。"""
    from insta_engine.state import ensure_default_workspace

    example = tmp_path / "cases" / "example-settings.json"
    example.parent.mkdir(parents=True)
    example.write_text(json.dumps({"caseId": "mine"}), encoding="utf-8")

    ensure_default_workspace(tmp_path)
    assert json.loads(example.read_text(encoding="utf-8"))["caseId"] == "mine", \
        "用户已有同名文件必须原样保留"
    ensure_default_workspace(tmp_path)  # 幂等：二次铺设不再改动
    assert json.loads(example.read_text(encoding="utf-8"))["caseId"] == "mine"
