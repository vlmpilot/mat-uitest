"""新版本检查（web_api/version_check.py）的单元测试——不真连 PyPI，全靠注入。"""

from __future__ import annotations

import pytest

from insta_engine.web_api import version_check as vc


def _set_result(latest: str | None) -> None:
    """直接写模块级缓存，绕过网络（_check_once 的真实网络行为不在单测范围）。"""
    vc._result = None if latest is None else {
        "latest": latest, "checked_at": "2026-10-14T00:00:00+00:00"}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _set_result(None)


def test_ver_tuple_segments():
    assert vc._ver_tuple("0.6.1") == (0, 6, 1)
    assert vc._ver_tuple("v0.6.10") == (0, 6, 10)
    assert vc._ver_tuple("0.6.1rc1") == (0, 6, 1)  # rc/后缀不参与比较
    assert vc._ver_tuple("garbage") == (0,)        # 解析不出数字按 0 段处理，不抛


_CUR = vc.__version__


def _bump(v: str) -> str:
    head, last = v.rsplit(".", 1)
    return f"{head}.{int(last) + 1}"


def test_has_update_semantics():
    _set_result(_bump(_CUR))
    assert vc.api_version_check()["has_update"] is True
    _set_result(_CUR)
    assert vc.api_version_check()["has_update"] is False   # 同版本不提示
    _set_result(_bump(_bump(_CUR)))
    assert vc.api_version_check()["has_update"] is True    # 段元数比较：0.6.10 > 0.6.1
    _set_result("0.0.0")
    assert vc.api_version_check()["has_update"] is False   # PyPI 更旧（回滚/本地更高）不提示
    _set_result(None)
    assert vc.api_version_check()["latest"] is None        # 查不到 → 静默不提示
    assert vc.api_version_check()["has_update"] is False


def test_no_check_env_disables(monkeypatch):
    _set_result("9.9.9")
    monkeypatch.setenv("MAT_NO_UPDATE_CHECK", "1")
    assert vc.api_version_check()["enabled"] is False
    assert vc.api_version_check()["has_update"] is False


def test_upgrade_command_channels(monkeypatch):
    monkeypatch.setattr(vc.sys, "prefix", "/home/u/.local/pipx/venvs/mat-uitest")
    assert vc._upgrade_command() == "pipx upgrade mat-uitest"
    monkeypatch.setattr(vc.sys, "prefix", "/home/u/.local/share/uv/tools/mat-uitest")
    assert vc._upgrade_command() == "uv tool upgrade mat-uitest"
    monkeypatch.setattr(vc.sys, "prefix", "/usr/local")
    monkeypatch.setattr(vc.platform, "system", lambda: "Darwin")
    assert vc._upgrade_command() == "pip install --upgrade mat-uitest"
    monkeypatch.setattr(vc.platform, "system", lambda: "Windows")
    assert vc._upgrade_command() == "py -m pip install --upgrade mat-uitest"


def test_start_background_check_env_off(monkeypatch):
    """MAT_NO_UPDATE_CHECK=1 时 start_background_check 直接返回（不建线程、零外呼）。"""
    monkeypatch.setenv("MAT_NO_UPDATE_CHECK", "1")
    vc.start_background_check()  # 若误建线程会去连 PyPI（1.5s 超时拖慢全量测试）——不抛即通过
