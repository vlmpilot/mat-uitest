"""启动落点哨兵（2026-10-31）：`launch` 之后那一屏是不是首页。

背景：2026-10-29 靶场 r3 里 `gen-183555-1..4` **整组跑在 tx 搜索页**上，一次带走 4 条、
把极差从 1.5pp 拉到 7.6pp。三道现成判据全都不认（`false_red` 要求判了 env-failure、
`off_page` 只认 WebView/离开 App、`classify_run_failures` 反而读成「A2 必失败」）。
阈值 0.293 是在 481 份存量屏幕上**标定**出来的，不是拍的（见 `insta_engine/landing.py`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_engine import runner as runner_mod
from insta_engine.dsl import TestCase
from insta_engine.landing import (
    HOME,
    HOME_SIM,
    MIN_SIGNALS,
    NOT_HOME,
    describe,
    home_profile,
    landing_kind,
)
from insta_engine.runner import Runner

PKG = "com.example.news"


def _ctl(cls: str, text: str = "", desc: str = "", rid: str = "") -> dict:
    out: dict = {"class": cls}
    if text:
        out["text"] = text
    if desc:
        out["desc"] = desc
    if rid:
        out["res-id"] = f"{PKG}:id/{rid}"
    return out


#: 真机形状的 tx 首页：频道栏 + 底栏 + 搜索框（有 rid）
_HOME = [
    _ctl("android.widget.FrameLayout", rid="channel_bar_item_container"),
    _ctl("android.widget.TextView", "要闻", rid="channelbar_text"),
    _ctl("android.widget.TextView", "国际", rid="channelbar_text"),
    _ctl("android.widget.FrameLayout", rid="home_channel_search_box"),
    _ctl("android.widget.TextView", rid="imgSearchIcon"),
    _ctl("android.widget.TextView", "新闻", rid="nav_btn_text"),
    _ctl("android.widget.TextView", "视频", rid="nav_btn_text"),
    _ctl("android.widget.TextView", "我的", rid="nav_btn_text"),
]

#: 真机形状的 tx 搜索结果页（Hippy 自绘：**零 rid**，文案全在 text）
_SEARCH = [
    _ctl("android.widget.TextView", "返回"),
    _ctl("android.widget.TextView", "清空"),
    _ctl("android.widget.TextView", "搜索"),
    _ctl("android.widget.TextView", "历史搜索"),
    _ctl("android.widget.TextView", "AI"),
    _ctl("android.widget.TextView", "新闻"),
]


def _index(pages: list[dict]) -> dict:
    return {"package": PKG, "pages": pages, "edges": [], "aliases": {}}


def _page(depth, controls, key="k0") -> dict:
    return {"page_key": key, "depth": depth, "controls": controls}


# -- 参考集：取 depth 最小的那一页 --------------------------------------------


def test_home_profile_uses_the_shallowest_page():
    """参考集只能是**启动落点**（depth 最小），不能被 depth=1 的详情页污染。

    实测 tx 图谱 59 页里 58 页都在 depth 1（详情/登录/我的/WebView/Hippy 全在）
    ⇒ 用 `home_labels_for(max_depth=1)` 会把参考集变成"这个 App 显示过的一切"。
    """
    idx = _index([_page(1, _SEARCH, "detail"), _page(0, _HOME, "root")])
    assert "rid:com.example.news:id/channel_bar_item_container" in home_profile(idx)
    assert "txt:历史搜索" not in home_profile(idx)


def test_home_profile_is_empty_without_depth():
    """老图谱没有 depth ⇒ 判不了，不猜（调用方必须静默）。"""
    assert home_profile(_index([{"page_key": "k", "controls": _HOME}])) == frozenset()
    assert home_profile({}) == frozenset()
    assert home_profile(None) == frozenset()


def test_home_profile_is_empty_when_the_root_page_has_no_chrome():
    idx = _index([_page(0, [_ctl("android.view.View")])])
    assert home_profile(idx) == frozenset()


# -- 三档 --------------------------------------------------------------------


def test_landing_home():
    profile = home_profile(_index([_page(0, _HOME)]))
    kind, score = landing_kind(_HOME, profile)
    assert kind == HOME
    assert score == 1.0


def test_landing_not_home_for_a_search_page():
    """真实现场：搜索页与首页的骨架交集为 0（文案 `搜索` 都不在首页骨架里）。"""
    profile = home_profile(_index([_page(0, _HOME)]))
    kind, score = landing_kind(_SEARCH, profile)
    assert kind == NOT_HOME
    assert score == 0.0


def test_unknown_when_profile_is_missing():
    assert landing_kind(_HOME, frozenset()) == (None, None)


def test_unknown_when_the_screen_has_no_controls_of_the_package():
    """`digest_page` 已按被测包过滤 ⇒ 空列表 = 这一屏没有被测 App 的控件。

    实测存量 8 条这种现场，`foreground` 全是**别的 App**（被切走、通知跳转等）
    ⇒ 记成"落点错"就是凭空造一个误报源。
    """
    profile = home_profile(_index([_page(0, _HOME)]))
    assert landing_kind([], profile) == (None, None)
    assert landing_kind(None, profile) == (None, None)


def test_unknown_when_too_few_signals_to_judge():
    """3 条信号判不了（存量 481 条里最少的真实页面是 4 条）——多半还在加载。"""
    profile = home_profile(_index([_page(0, _HOME)]))
    tiny = _SEARCH[: MIN_SIGNALS - 1]
    assert len(tiny) == MIN_SIGNALS - 1
    assert landing_kind(tiny, profile) == (None, None)


# -- 阈值 --------------------------------------------------------------------


def test_threshold_sits_in_the_calibrated_gap():
    """锁住标定结论，防止有人"顺手调一下"。

    图谱参考下两簇是 0.184 → 0.371；把参考换成首页另一次观测后的稳健性下界是
    0.233 → 0.353。阈值必须落在**两者交集**里，否则重建图谱就会顶穿。
    """
    assert 0.233 < HOME_SIM < 0.353


def test_a_home_page_with_extra_chrome_is_still_home():
    """首页多出几条 chrome（7x24 音量条、分享角标…）不该被判成"不是首页"。

    实测这条边界：`gen-184020-1` 的 7x24 页只拿到 0.371（簇内最低），仍是首页。
    """
    profile = home_profile(_index([_page(0, _HOME)]))
    noisy = _HOME + [
        _ctl("android.widget.TextView", "音量开关"),
        _ctl("android.widget.TextView", "分享"),
        _ctl("android.widget.TextView", "更多"),
        _ctl("android.widget.TextView", "智声"),
    ]
    kind, score = landing_kind(noisy, profile)
    assert kind == HOME, f"多几条 chrome 就被判成落错页（score={score}）"


def test_describe():
    assert describe(None) == ""
    assert describe(HOME)
    assert "首页" in describe(NOT_HOME)


# -- 跑起来真的会写进 run.json ------------------------------------------------

_SEARCH_XML = (
    "<hierarchy>"
    + "".join(
        f'<node class="android.widget.TextView" package="{PKG}" text="{t}"'
        f' bounds="[46,{138 + i * 90}][1034,{207 + i * 90}]" clickable="true"/>'
        for i, t in enumerate(["返回", "清空", "搜索", "历史搜索", "AI", "新闻"])
    )
    + "</hierarchy>"
)

_HOME_XML = (
    "<hierarchy>"
    '<node class="android.widget.FrameLayout" package="com.example.news"'
    ' resource-id="com.example.news:id/channel_bar_item_container"'
    ' clickable="true" bounds="[0,337][1080,430]"/>'
    '<node class="android.widget.TextView" package="com.example.news" text="要闻"'
    ' resource-id="com.example.news:id/channelbar_text" bounds="[46,340][200,420]"/>'
    '<node class="android.widget.FrameLayout" package="com.example.news"'
    ' resource-id="com.example.news:id/home_channel_search_box"'
    ' clickable="true" bounds="[0,100][1080,200]"/>'
    '<node class="android.widget.TextView" package="com.example.news" text="新闻"'
    ' resource-id="com.example.news:id/nav_btn_text" bounds="[0,2200][270,2300]"/>'
    '<node class="android.widget.TextView" package="com.example.news" text="视频"'
    ' resource-id="com.example.news:id/nav_btn_text" bounds="[270,2200][540,2300]"/>'
    '<node class="android.widget.TextView" package="com.example.news" text="我的"'
    ' resource-id="com.example.news:id/nav_btn_text" bounds="[810,2200][1080,2300]"/>'
    "</hierarchy>"
)


class _Driver:
    """最小驱动面：只够跑通 launch 步（含启动噪声清理与前台守卫）。"""

    platform = "android"
    serial = "SERIAL-1"

    def __init__(self, xml: str, fg: str = PKG):
        self._xml = xml
        self._fg = fg

    def dump(self):
        return self._xml

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def wake_and_unlock(self):
        return None

    def start_keepalive(self):
        return None

    def stop_keepalive(self):
        return None

    def start_evidence(self):
        return None

    def reconnect(self):
        return None

    def foreground_app(self):
        return {"package": self._fg}

    def app_start(self, package, activity=None):
        return None

    def app_stop(self, package):
        return None

    def find(self, by, value, timeout=0):
        class _Absent:
            exists = False

            def click(self):
                return None
        return _Absent()

    def press(self, key):
        return None


def _launch_case(**params) -> TestCase:
    return TestCase.model_validate({
        "caseId": "landing",
        "name": "启动落点",
        "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch",
                   "params": {"package": PKG, **params}}],
    })


@pytest.fixture(autouse=True)
def _fast_and_quiet(monkeypatch):
    """别真跑 adb、别真等 0.8s、别真清启动噪声（那会多 dump 好几次）。"""
    monkeypatch.setattr(runner_mod, "am_start_w",
                        lambda serial, package, activity=None: {"launch_ms": 1234})
    monkeypatch.setattr(runner_mod, "_pause", lambda seconds: None)
    monkeypatch.setattr(Runner, "_skip_splash_ad", lambda self, package=None: None)


def _write_index(tmp_path: Path) -> Path:
    import json

    path = tmp_path / "idx.json"
    path.write_text(json.dumps(_index([_page(0, _HOME)]), ensure_ascii=False),
                    encoding="utf-8")
    return path


def test_runner_records_not_home_landing(tmp_path):
    """落点是搜索页 ⇒ `landing=not-home` 且带分数；**判定不变**（仍 passed）。"""
    run = Runner(_Driver(_SEARCH_XML), tmp_path, index_path=_write_index(tmp_path)).run(
        _launch_case())
    s1 = run.steps[0]
    assert s1.landing == NOT_HOME
    assert s1.landing_score == 0.0
    assert s1.status == "passed", "哨兵只打标，不许改判定"
    assert s1.category is None


def test_runner_records_home_landing(tmp_path):
    """分数不必是 1.0——真实首页的骨架也不会与图谱根页逐条相同。

    这正是需要阈值的理由：`digest_page` 的粒度（同 rid 多文案去重、纯容器过滤）
    与图谱落库时的粒度不会完全一致，所以只断言"过阈值"。
    """
    run = Runner(_Driver(_HOME_XML), tmp_path, index_path=_write_index(tmp_path)).run(
        _launch_case())
    assert run.steps[0].landing == HOME
    assert run.steps[0].landing_score >= HOME_SIM


def test_runner_is_silent_without_a_graph(tmp_path):
    """没图谱 ⇒ 不写字段。"没报"就是"没证据"，不是"没问题"（与 L5 同口径）。"""
    run = Runner(_Driver(_SEARCH_XML), tmp_path,
                 index_path=tmp_path / "nope.json").run(_launch_case())
    assert run.steps[0].landing is None
    assert run.steps[0].landing_score is None


def test_runner_is_silent_when_the_screen_has_no_controls_of_the_package(tmp_path):
    """整屏不是被测 App 的控件（别的 App 在前台 / 纯 WebView）⇒ 判不了，静默。"""
    run = Runner(_Driver("<hierarchy/>"), tmp_path,
                 index_path=_write_index(tmp_path)).run(_launch_case())
    assert run.steps[0].landing is None


# -- 冷启动留痕 --------------------------------------------------------------


def test_cold_start_is_recorded(tmp_path):
    """`app_stop` 成功 ⇒ `cold_start=ok`。此前这里是 `except: pass`，
    于是"没执行"与"执行了但没清掉残留"在产物里长得一模一样。"""
    run = Runner(_Driver(_HOME_XML), tmp_path, index_path=_write_index(tmp_path)).run(
        _launch_case())
    assert run.steps[0].cold_start == "ok"


def test_cold_start_failure_is_visible(tmp_path):
    """`app_stop` 抛异常 ⇒ 记 `failed`（仍继续热启动，不改判定）。"""
    class _NoStop(_Driver):
        def app_stop(self, package):
            raise RuntimeError("am force-stop 返回非零")

    run = Runner(_NoStop(_HOME_XML), tmp_path, index_path=_write_index(tmp_path)).run(
        _launch_case())
    assert run.steps[0].cold_start == "failed"
    assert run.steps[0].status == "passed", "冷启动是增益，失败不许判死"


def test_cold_start_skipped_when_disabled(tmp_path):
    """用例显式 `cold: false` ⇒ 记 `skipped`（不是 ok，也不是 failed）。"""
    run = Runner(_Driver(_HOME_XML), tmp_path, index_path=_write_index(tmp_path)).run(
        _launch_case(cold=False))
    assert run.steps[0].cold_start == "skipped"
