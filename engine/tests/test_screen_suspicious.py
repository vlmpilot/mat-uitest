"""白屏/黑屏启发：**必须能穿过系统栏找到"内容区一片空白"**（T76）。

## 为什么这条测试值得单独存在

`_screen_suspicious` 是"**发现异常**"（崩溃/白屏/离场）三根支柱里的一根，
但它的原判据（**整屏**灰度极差 < 6）**在真机上永远不成立** ——
截图必然含 **状态栏 + ActionBar + 导航栏** ⇒ 整屏极差恒为 255。

**实测（2026-10-07，靶场白屏页）**：

| 区域 | 灰度极差 | 原判据 |
|---|---|---|
| 整屏 | **255** | ❌ 判不出 |
| 裁掉上下 7% | 246 | ❌ |
| 裁掉上下 10% | 224 | ❌ |
| **中间 60%（内容区）** | **0** | ✅ |

⇒ 改成"**最长的近均匀行带 ≥ 屏高 60%**"。本文件锁住改法，也锁住**不能误报**。
⚠️ 合成图里**刻意画上状态栏与 ActionBar** —— 只用"纯白图"测的话，
旧判据也能过，**这条测试就白写了**（同族于台账 D6：护栏必须真能抓住它该抓的）。
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL", reason="白屏启发依赖 Pillow（可选依赖）")

from PIL import Image, ImageDraw  # noqa: E402

from insta_engine.explore import (  # noqa: E402
    SUSPICIOUS_BAND_RATIO,
    _screen_suspicious,
)

W, H = 360, 780


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _phone_shell(content_ratio: float, content_color: int = 255) -> Image.Image:
    """画一张"真机截图"：**状态栏 + ActionBar + 内容区 + 导航栏**。

    `content_ratio` = 内容区占屏高的比例；内容区之外用深色（模拟系统栏）。
    """
    img = Image.new("L", (W, H), 20)              # 深色背景 = 系统栏/ActionBar 的近似
    top = int(H * (1 - content_ratio) / 2)
    bottom = H - top
    ImageDraw.Draw(img).rectangle([0, top, W, bottom], fill=content_color)
    return img


def test_blank_content_is_detected_despite_system_bars():
    """**核心**：内容区纯白 + 上下有系统栏 ⇒ 必须判成 `white-screen`。

    这正是旧判据判不出来的场景（整屏极差 255）。
    """
    img = _phone_shell(content_ratio=0.85, content_color=255)
    assert _screen_suspicious(_png(img)) == "white-screen"


def test_blank_dark_content_is_black_screen():
    img = _phone_shell(content_ratio=0.85, content_color=0)
    assert _screen_suspicious(_png(img)) == "black-screen"


def test_fully_uniform_image_still_works():
    """整屏均匀（旧判据的唯一覆盖场景）**不能因为改判据而丢掉**。"""
    assert _screen_suspicious(_png(Image.new("L", (W, H), 255))) == "white-screen"
    assert _screen_suspicious(_png(Image.new("L", (W, H), 0))) == "black-screen"


def test_normal_page_is_not_flagged():
    """**反例**：正常页面（内容铺满）**不能**被判成白屏 —— 否则会到处误报。"""
    img = Image.new("L", (W, H), 245)
    draw = ImageDraw.Draw(img)
    for y in range(60, H - 60, 40):               # 每 40px 一条深色内容
        draw.rectangle([20, y, W - 20, y + 14], fill=40)
    assert _screen_suspicious(_png(img)) is None


def test_band_below_ratio_is_not_flagged():
    """**边界反例**：空白带**不够长**时不该判可疑（阈值是 `SUSPICIOUS_BAND_RATIO`）。"""
    just_below = SUSPICIOUS_BAND_RATIO - 0.1
    img = _phone_shell(content_ratio=just_below, content_color=255)
    assert _screen_suspicious(_png(img)) is None, (
        f"空白带 {just_below:.0%} < 阈值 {SUSPICIOUS_BAND_RATIO:.0%} 时不该判可疑")


def test_unreadable_bytes_do_not_raise():
    """截图读不了 ⇒ 返回 None（**宁可漏报也不误报**，且绝不能抛）。"""
    assert _screen_suspicious(b"not a png at all") is None
