"""视觉兜底「为什么没采纳」必须可读（2026-10-26）。

背景：`locate()` 只返回坐标或 `None`，调用方原来只能把三种完全不同的情况压成同一句
「未命中或未启用」——① 模型说没找到 ② 找到了但置信度不够被拒 ③ 压根没调用。
用户跑完一条用例，无法判断本地模型到底被调过没有（实测：报告里只有那一句，
只能靠翻服务端日志确认）。现在每次判定都留 `last_result()`。
"""

from __future__ import annotations

import io

from PIL import Image

from insta_engine import vision


def _png(w: int = 400, h: int = 800) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_found_false_is_named():
    assert vision.parse_vision_response({"found": False}, _png()) is None
    r = vision.last_result()
    assert r["ok"] is False
    assert "found=false" in r["reason"], r


def test_low_confidence_says_which_gate():
    parsed = vision.parse_vision_response(
        {"found": True, "confidence": 0.5, "x": 0.5, "y": 0.5}, _png(), tree_evidence=True
    )
    assert parsed is None
    r = vision.last_result()
    assert "门禁" in r["reason"] and "0.7" in r["reason"], r


def test_tree_evidence_raises_gate_in_reason():
    """无在场证据时门禁升到 0.8，reason 必须说清是哪个门禁拒的。"""
    assert vision.parse_vision_response(
        {"found": True, "confidence": 0.75, "x": 0.5, "y": 0.5}, _png(), tree_evidence=False
    ) is None
    assert "0.8" in vision.last_result()["reason"]


def test_hit_is_marked_ok():
    parsed = vision.parse_vision_response(
        {"found": True, "confidence": 0.95, "x": 0.5, "y": 0.25}, _png()
    )
    assert parsed == (200, 200)
    assert vision.last_result() == {**vision.last_result(), "ok": True, "reason": "命中"}


def test_cache_hit_says_model_not_called(monkeypatch):
    """缓存命中不调用模型，reason 必须明确——否则会被误读成"模型又答了一次"。"""
    monkeypatch.setattr(vision, "_CACHE", vision.OrderedDict())
    calls = {"n": 0}

    def fake_complete(*a, **kw):
        calls["n"] += 1
        return '{"found": true, "confidence": 0.9, "x": 0.5, "y": 0.5}'

    monkeypatch.setattr("insta_engine.ai_client.complete_task_text", fake_complete)

    class _Client:
        pass

    png = _png()
    assert vision.locate(_Client(), png, "", "登录", cache_key="k") == (200, 400)
    assert vision.locate(_Client(), png, "", "登录", cache_key="k") == (200, 400)
    assert calls["n"] == 1, "第二次应命中缓存"
    assert "缓存" in vision.last_result()["reason"]


def test_runner_surfaces_model_reason(tmp_path, monkeypatch):
    """runner 层：模型判未命中时，步骤 error 里要出现模型侧原因，而不是笼统旧文案。"""
    import insta_engine.vision as vision_mod
    from insta_engine.dsl import TestCase
    from insta_engine.runner import Runner

    class _ShotDriver:
        def screenshot_pil(self):
            return Image.new("RGB", (4, 4), "white")

        def dump(self) -> str:
            return "<hierarchy/>"

    def fake_locate(client, image, intent, target, cache_key=None,
                    tree_evidence=False, before_call=None):
        vision_mod.parse_vision_response({"found": False}, image, tree_evidence=tree_evidence)
        return None

    monkeypatch.setenv("GEN_PROMPT_MODE", "local")
    monkeypatch.setenv("GEN_VL_MODEL", "vl")
    monkeypatch.setenv("GEN_API_KEY", "k")
    monkeypatch.setattr(vision_mod, "enabled", lambda: True)
    monkeypatch.setattr(vision_mod, "default_client", lambda: object())
    monkeypatch.setattr(vision_mod, "locate", fake_locate)

    case = TestCase.model_validate({
        "caseId": "vision_reason",
        "name": "原因可读",
        "target": {"package": "com.insta.demo"},
        "steps": [{"id": "s1", "action": "tap",
                   "target": {"strategy": "text", "value": "登录"}}],
    })
    runner = Runner(_ShotDriver(), tmp_path)
    assert runner._vision_locate(case.steps[0], {}) is None
    assert "found=false" in runner.vision_reason, runner.vision_reason
    assert "未命中或未启用" not in runner.vision_reason
