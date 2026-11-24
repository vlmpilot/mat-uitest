"""视觉断言与语义断言（评审 #14）。

两条设计约束必须在测试里钉住：
1. `visual-changed` **不调模型**、只比画面指纹——自绘界面里控件属性断言什么也证明不了，
   这是唯一确定性判据；
2. `semantic` **显式开启才生效**，低置信不算成立——它是概率性的，不能默认信任。
"""

from __future__ import annotations

import os

import pytest

from insta_engine import semantic, vision
from insta_engine.dsl import Assertion, AssertionType, TestCase
from insta_engine.runner import Runner


class _FrameDriver:
    """截图可控的桩：按队列返回不同画面（模拟"点了之后界面变了/没变"）。"""

    platform = "android"
    serial = None

    def __init__(self, frames: list[tuple[int, int, int]]):
        self._frames = list(frames)

    def screenshot_pil(self):
        from PIL import Image

        color = self._frames.pop(0) if self._frames else (255, 255, 255)
        return Image.new("RGB", (1080, 2340), color)

    # runner 在 wait_stable 里会 dump
    def dump(self) -> str:
        return '<hierarchy rotation="0"></hierarchy>'


def _case(assertion: dict) -> TestCase:
    return TestCase.model_validate({
        "caseId": "case_vis",
        "steps": [{"id": "s1", "action": "wait", "params": {"ms": 1},
                   "assertions": [assertion]}],
    })


def test_visual_changed_passes_when_frame_differs(tmp_path):
    # 动作前白屏、动作后黑屏 ⇒ 变了
    drv = _FrameDriver([(255, 255, 255), (0, 0, 0)])
    run = Runner(drv, tmp_path).run(_case({"type": "visual-changed"}))
    assert run.steps[0].status == "passed", run.steps[0].error


def test_visual_changed_fails_when_frame_identical(tmp_path):
    drv = _FrameDriver([(128, 128, 128), (128, 128, 128)])
    run = Runner(drv, tmp_path).run(_case({"type": "visual-changed"}))
    assert run.steps[0].status == "failed"
    assert "画面与动作前相同" in (run.steps[0].error or "")


def test_visual_changed_is_end_to_end_deterministic(tmp_path):
    """同样的输入跑两次结论必须一致——视觉断言不引入随机性（不调模型）。"""
    outs = []
    for _ in range(2):
        drv = _FrameDriver([(10, 20, 30), (200, 210, 220)])
        outs.append(Runner(drv, tmp_path).run(_case({"type": "visual-changed"})).steps[0].status)
    assert outs == ["passed", "passed"]


def test_semantic_is_off_by_default_and_says_how_to_enable(tmp_path, monkeypatch):
    monkeypatch.delenv(semantic.ENV_FLAG, raising=False)
    drv = _FrameDriver([(1, 1, 1)])
    run = Runner(drv, tmp_path).run(_case({"type": "semantic", "expected": "展示了下单成功"}))
    assert run.steps[0].status == "failed"
    err = run.steps[0].error or ""
    assert "未启用" in err and semantic.ENV_FLAG in err, f"要说清怎么开：{err}"


def test_semantic_low_confidence_is_not_accepted(tmp_path, monkeypatch):
    """模型说成立但置信度不够 ⇒ 不成立。断言语义里"不确定"等于"没证明"。"""
    monkeypatch.setenv(semantic.ENV_FLAG, "1")
    monkeypatch.setenv("GEN_VL_MODEL", "qwen3-vl-plus")
    monkeypatch.setenv("GEN_API_KEY", "k")
    monkeypatch.setattr(vision, "enabled", lambda: True)

    class _Client:
        pass

    monkeypatch.setattr(vision, "default_client", lambda: _Client())
    monkeypatch.setattr("insta_engine.ai_client.complete_task_text",
                        lambda *a, **k: '{"ok": true, "confidence": 0.4, "reason": "看不清"}')
    semantic.reset()

    drv = _FrameDriver([(5, 5, 5)])
    run = Runner(drv, tmp_path).run(_case({"type": "semantic", "expected": "下单成功"}))
    assert run.steps[0].status == "failed"
    assert "置信度" in (run.steps[0].error or "")
    semantic.reset()


def test_semantic_high_confidence_passes_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv(semantic.ENV_FLAG, "1")
    monkeypatch.setattr(vision, "enabled", lambda: True)
    monkeypatch.setattr(vision, "default_client", lambda: object())
    calls = []

    def fake_complete(*a, **k):
        calls.append(1)
        return '{"ok": true, "confidence": 0.93, "reason": "页面有『下单成功』绿色勾选"}'

    monkeypatch.setattr("insta_engine.ai_client.complete_task_text", fake_complete)
    semantic.reset()

    drv = _FrameDriver([(7, 7, 7)])
    run = Runner(drv, tmp_path).run(_case({"type": "semantic", "expected": "下单成功"}))
    assert run.steps[0].status == "passed"
    assert len(calls) == 1

    # 同屏 + 同断言 ⇒ 命中缓存，不再调用
    semantic.judge(_FrameDriver([(7, 7, 7)]), "下单成功")
    assert len(calls) == 1, "同一屏同一句话不该重复调用模型"
    semantic.reset()


def test_assertion_strength_marks_semantic_as_model_judged():
    from insta_engine.assertion_strength import classify_step

    layer, reason = classify_step({"action": "wait",
                                   "assertions": [{"type": "semantic",
                                                   "expected": "展示了下单成功"}]})
    assert layer == "strong"
    assert "模型判定" in reason, f"语义断言要单独标出来源：{reason}"

    layer2, reason2 = classify_step({"action": "wait", "assertions": [{"type": "visual-changed"}]})
    assert layer2 == "strong" and "visual-changed" in reason2
