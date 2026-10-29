"""送模型的截图可以缩，但**坐标换算必须用原图尺寸**（2026-10-26）。

背景：本地小模型（Qwen3-VL-8B-4bit 等）在 1080×2400 上单次要 7~8 秒，把长边压到
1280 可降到 3~4 秒。但 `locate()` 原本把**同一份 bytes** 既发给模型、又拿去量尺寸：
一旦缩图，`parse_vision_response` 就会用缩略图的宽度去乘比例，像素坐标整体偏小
（1080 宽缩到 576，x=0.5 会算成 288 而不是 540）——**看起来只差个数字，实际是点错
位置**。这组测试锁住"缩图只影响送什么、不影响怎么换算"。

顺带锁住三条退化行为：默认不缩（既有云端行为不变）、未超上限不重编码、
环境变量写坏时退回原图（缩图是优化，不能因为它让兜底挂掉）。
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from insta_engine import vision


def _png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "#203040").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例从"未配置"开始，并清掉跨用例的定位缓存（缓存键是树哈希，会串）。"""
    monkeypatch.delenv(vision.MAX_EDGE_ENV, raising=False)
    vision._CACHE.clear()


# -- prepare_image：只决定"发什么" ---------------------------------------------

def test_default_keeps_original_bytes():
    """默认（未设 MAT_VL_MAX_EDGE）零拷贝、零重编码——云端用户行为完全不变。"""
    png = _png((1080, 2400))
    sent, info = vision.prepare_image(png)
    assert sent is png
    assert info["scaled"] is False


def test_long_edge_is_shrunk_to_limit(monkeypatch):
    monkeypatch.setenv(vision.MAX_EDGE_ENV, "1280")
    sent, info = vision.prepare_image(_png((1080, 2400)))

    assert info["scaled"] is True
    assert info["original"] == (1080, 2400)
    assert max(info["sent"]) == 1280
    assert Image.open(io.BytesIO(sent)).size == info["sent"], "元信息须与实际发出的图一致"
    assert len(sent) < len(_png((1080, 2400))), "缩了就该更小"


def test_under_limit_is_not_touched(monkeypatch):
    monkeypatch.setenv(vision.MAX_EDGE_ENV, "1280")
    png = _png((400, 300))
    sent, info = vision.prepare_image(png)
    assert sent is png
    assert info["scaled"] is False


def test_bad_value_falls_back_to_original(monkeypatch):
    """坏值不猜：宁可慢，也不让「缩图」这个优化把兜底搞挂。"""
    monkeypatch.setenv(vision.MAX_EDGE_ENV, "abc")
    png = _png((1080, 2400))
    sent, info = vision.prepare_image(png)
    assert sent is png
    assert info["scaled"] is False


def test_zero_disables_shrinking(monkeypatch):
    monkeypatch.setenv(vision.MAX_EDGE_ENV, "0")
    png = _png((1080, 2400))
    assert vision.prepare_image(png)[0] is png


# -- locate：换算用原图 ---------------------------------------------------------

def _fake_model(monkeypatch, payload: dict, capture: dict) -> None:
    """替换真正的模型调用，并记录**实际发出去的图片字节**。"""

    def fake_complete(client, task, variables, context, images=None, **kwargs):
        capture["task"] = task
        capture["variables"] = variables
        capture["images"] = list(images or [])
        return json.dumps(payload)

    monkeypatch.setattr("insta_engine.ai_client.complete_task_text", fake_complete)


_ANSWER = {"found": True, "confidence": 0.93, "x": 0.5, "y": 0.25}


def test_locate_converts_with_original_size_when_shrunk(monkeypatch):
    """核心断言：缩图后坐标仍按 1080×2400 换算 ⇒ (540,600)，不是 (288,320)。"""
    monkeypatch.setenv(vision.MAX_EDGE_ENV, "1280")
    capture: dict = {}
    _fake_model(monkeypatch, _ANSWER, capture)

    point = vision.locate(object(), _png((1080, 2400)), "搜索", "搜索")

    assert point == (540, 600)
    sent = capture["images"][0]
    assert Image.open(io.BytesIO(sent)).size == (576, 1280), "发给模型的应是缩略图"
    assert vision.last_image_info()["scaled"] is True


def test_locate_without_shrink_is_unchanged(monkeypatch):
    """回归：不配上限时，发出去的还是原图、结果与旧行为一致。"""
    capture: dict = {}
    _fake_model(monkeypatch, _ANSWER, capture)
    png = _png((1080, 2400))

    assert vision.locate(object(), png, "", "搜索") == (540, 600)
    assert capture["images"][0] is png
    assert vision.last_image_info()["scaled"] is False


def test_shrink_does_not_bypass_confidence_gate(monkeypatch):
    """缩图不该顺手放松红线：低置信照样拒。"""
    monkeypatch.setenv(vision.MAX_EDGE_ENV, "1280")
    _fake_model(monkeypatch, {**_ANSWER, "confidence": 0.45}, {})

    assert vision.locate(object(), _png((1080, 2400)), "", "搜索") is None


def test_locate_passes_vision_task_and_variables(monkeypatch):
    """缩图改造不该动任务名与变量契约（服务端按 task 选 prompt）。"""
    capture: dict = {}
    _fake_model(monkeypatch, _ANSWER, capture)
    vision.locate(object(), _png((200, 400)), "搜索商品", "搜索框")

    assert capture["task"] == "vision-locate"
    assert capture["variables"] == {"intent": "搜索商品", "target": "搜索框"}
