"""生成路由（`MAT_ROUTE`）：默认自部署，显式 `cloud` 才走云端模型商。

这里的断言都在钉**用户能不能知道自己的内容去了哪**：
产品承诺是"本地执行、截图与日志只存本机"，默认值就是事实标准，
而 `mat status` 必须如实说出文本与截图这两条路各自通向哪里。
"""

from __future__ import annotations

from insta_engine import route
from insta_engine.__main__ import _print_routing


def test_default_is_auto(monkeypatch):
    monkeypatch.delenv("MAT_ROUTE", raising=False)
    assert route.preference() == route.AUTO
    assert route.is_explicit() is False
    assert route.unknown_value() == ""


def test_values_are_normalized(monkeypatch):
    for raw, want in (("cloud", route.CLOUD), ("CLOUD ", route.CLOUD),
                      ("local", route.LOCAL), ("auto", route.AUTO), ("", route.AUTO)):
        monkeypatch.setenv("MAT_ROUTE", raw)
        assert route.preference() == want, raw


def test_typo_falls_back_to_auto_and_is_reported(monkeypatch):
    """拼错走安全方向（不外发），但要能被 doctor 报出来，别静默。"""
    monkeypatch.setenv("MAT_ROUTE", "clod")
    assert route.preference() == route.AUTO
    assert route.unknown_value() == "clod"


def test_route_line_cloud(monkeypatch, capsys):
    monkeypatch.setenv("MAT_ROUTE", "cloud")
    _print_routing({"routing": {"text_local": True, "vision_local": True}})
    out = capsys.readouterr().out
    assert "云端模型服务商" in out
    assert "截图都会发给" in out


def test_route_line_says_screenshots_still_leave(monkeypatch, capsys):
    """默认路由下必须点明截图仍会外发——一句笼统的"不外发"就是假话。"""
    monkeypatch.delenv("MAT_ROUTE", raising=False)
    _print_routing({"routing": {"text_local": True, "vision_local": False}})
    out = capsys.readouterr().out
    assert "文本在本站自部署模型" in out
    assert "截图会外发" in out


def test_route_line_all_local(monkeypatch, capsys):
    """全本地要明确"不可用就报错"——不写这半句，用户会以为会自动改用云端。"""
    monkeypatch.delenv("MAT_ROUTE", raising=False)
    _print_routing({"routing": {"text_local": True, "vision_local": True}})
    out = capsys.readouterr().out
    assert "都不外发" in out
    assert "不会改走云端" in out


def test_route_line_flags_local_refused_for_private(monkeypatch, capsys):
    """`MAT_ROUTE=local` + private：服务端会拒绝，status 要提前说，别等生成失败。"""
    monkeypatch.setenv("MAT_ROUTE", "local")
    _print_routing({"routing": {"text_local": False, "vision_local": False}})
    assert "会被拒绝" in capsys.readouterr().out


def test_route_line_silent_without_server_facts(monkeypatch, capsys):
    """老服务端没有 `routing` 字段：不猜、也不误导。"""
    monkeypatch.delenv("MAT_ROUTE", raising=False)
    _print_routing({})
    assert capsys.readouterr().out == ""
