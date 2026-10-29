"""R16（2026-10-29）：失败现场「已换页」的打标。

背景：`locate-fail` 里混着「定位值写错」（用例问题）和「这一屏根本不是用例假设的那一屏」
（插屏广告把微信小程序拉起来 / App 内换成全屏 WebView 落地页）。两者在 `run.json` 里
长得一样，于是首跑通过率判读会把后者算成用例问题。这里的判据是**在 355 份存量失败现场上
标定出来的**（见 `insta_engine/off_page.py` 的模块 docstring），不是拍脑袋的阈值。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine.dsl import TestCase
from insta_engine.off_page import LEFT_APP, WEBVIEW_PAGE, describe, off_page_kind
from insta_engine.runner import Runner

PKG = "com.tencent.news"


def _ctl(cls: str, text: str = "", desc: str = "") -> dict:
    out = {"class": cls}
    if text:
        out["text"] = text
    if desc:
        out["desc"] = desc
    return out


#: 真实现场（`artifacts/2026-10-28-d3-after-rebuild2/r2`）：广告落地页 = 标题栏 + 一个 WebView
_AD_LANDING = [
    _ctl("android.widget.TextView", "返回", "返回"),
    _ctl("android.widget.LinearLayout"),
    _ctl("android.widget.TextView", "更多", "更多"),
    _ctl("android.webkit.WebView", "腕表回收中心"),
    _ctl("android.widget.Image", "img"),
]


def test_left_app_when_foreground_is_another_package():
    assert off_page_kind(PKG, [], "com.tencent.mm") == LEFT_APP


def test_left_app_wins_over_the_webview_signature():
    """微信小程序里也有 WebView —— 换了 App 时先说"换了 App"，别含糊成"App 内换页"。"""
    assert off_page_kind(PKG, _AD_LANDING, "com.tencent.mm") == LEFT_APP


def test_webview_page_when_only_one_webview_and_few_controls():
    assert off_page_kind(PKG, _AD_LANDING, PKG) == WEBVIEW_PAGE


def test_webview_signature_does_not_depend_on_foreground():
    """取不到前台包名时：**仍能判「App 内换页」**，但**判不了「换了 App」**。

    为什么两者不一样：`left-app` 的判据就是"前台 != 被测包"，前台未知时无从谈起；
    而"整屏只剩一个带标题的 WebView"是从**控件树**读出来的，与谁在前台无关。
    别为了对称把后者也砍掉——那会让「前台取不到」的批次白丢一条可用证据。
    """
    assert off_page_kind(PKG, _AD_LANDING, "") == WEBVIEW_PAGE
    assert off_page_kind(PKG, _AD_LANDING, "com.tencent.mm") == LEFT_APP


def test_no_flag_without_package():
    assert off_page_kind("", _AD_LANDING, "") is None


def test_no_flag_for_a_page_without_webview():
    controls = [_ctl("android.widget.TextView", "设置") for _ in range(5)]
    assert off_page_kind(PKG, controls, PKG) is None


def test_no_flag_for_an_article_page():
    """文章详情页也是「标题栏 + 1 个 WebView」，但控件数 ≥30（存量实测 35）⇒ 不判。

    这条是**边界**：阈值 30 来自存量两簇（≤22 广告 / ≥35 正常页）之间的空隙，
    压到 35 会把正常详情页卷进来，放到 25 会漏掉「淘宝精选」那种 22 控件的广告页。
    """
    controls = _AD_LANDING + [_ctl("android.widget.TextView", f"正文{i}") for i in range(25)]
    assert len(controls) >= 30
    assert off_page_kind(PKG, controls, PKG) is None


def test_no_flag_when_the_webview_has_no_title():
    """空白 WebView 更像"还在加载"，不该被说成"被换页"。"""
    controls = [_ctl("android.webkit.WebView"), _ctl("android.widget.TextView", "返回")]
    assert off_page_kind(PKG, controls, PKG) is None


def test_no_flag_with_two_webviews():
    controls = [_ctl("android.webkit.WebView", "标题"), _ctl("android.webkit.WebView", "另一个")]
    assert off_page_kind(PKG, controls, PKG) is None


def test_no_flag_for_an_empty_control_list():
    """前台就是被测 App 但一个控件都没有 ⇒ 可能还在加载，信息量太低，不判。"""
    assert off_page_kind(PKG, [], PKG) is None


def test_describe_is_empty_for_none():
    assert describe(None) == ""
    assert "App" in describe(LEFT_APP)
    assert "WebView" in describe(WEBVIEW_PAGE)


# -- 跑起来真的会写进 run.json ------------------------------------------------

_AD_XML = (
    "<hierarchy>"
    '<node class="android.widget.TextView" package="com.tencent.news" text="返回"'
    ' resource-id="com.tencent.news:id/back" bounds="[46,138][115,207]" clickable="true"/>'
    '<node class="android.webkit.WebView" package="com.tencent.news" text="腕表回收中心"'
    ' bounds="[0,243][1080,2214]"/>'
    "</hierarchy>"
)


class _Absent:
    exists = False

    def wait(self, timeout=0, raise_error=False):
        return False


class _AdDriver:
    """目标控件找不到 + 这一屏是广告落地页（真实现场的形状）。"""

    serial = None
    platform = "android"

    def __init__(self, foreground: str):
        self.foreground = foreground

    def dump(self):
        return _AD_XML

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def foreground_app(self):
        return {"package": self.foreground}

    def find(self, by, value, timeout, index=None):
        return _Absent()

    def press(self, key):
        return None

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


def _case() -> TestCase:
    return TestCase.model_validate({
        "caseId": "r16", "name": "换页打标",
        "target": {"package": "com.tencent.news"},
        "steps": [{"id": "s1", "action": "tap",
                   "target": {"strategy": "text", "value": "我的"}}],
    })


def test_runner_records_webview_page(tmp_path):
    run = Runner(_AdDriver(foreground="com.tencent.news"), tmp_path).run(_case())
    s1 = run.steps[0]
    assert s1.status == "failed"
    assert s1.off_page == WEBVIEW_PAGE
    # 只打标，不改判定：类别与状态都必须维持原样，否则历史基线不可比
    assert s1.category == "locate-fail"


def test_runner_records_left_app(tmp_path):
    run = Runner(_AdDriver(foreground="com.tencent.mm"), tmp_path).run(_case())
    assert run.steps[0].off_page == LEFT_APP


def test_runner_leaves_off_page_empty_on_a_normal_page(tmp_path):
    """正常页面（无 WebView）不写这个字段 —— "没报"就是"没证据"，不是"没问题"。"""
    class _Plain(_AdDriver):
        def dump(self):
            return ("<hierarchy>"
                    '<node class="android.widget.TextView" package="com.tencent.news"'
                    ' text="设置" bounds="[46,138][115,207]" clickable="true"/>'
                    "</hierarchy>")

    run = Runner(_Plain(foreground="com.tencent.news"), tmp_path).run(_case())
    assert run.steps[0].off_page is None
