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


# -- 文案审计（2026-11-02：只观测不拦截）------------------------------------
#
# 实测的幻觉样本是：found=true / confidence=0.95 / 坐标 (0.137,0.383) / desc 原样
# 回显 prompt 里的 "元素简述"。置信度和坐标都不可信，**文案才是破绽**。


def test_extract_fields_reads_desc():
    assert vision.extract_fields('{"desc": "齿轮图标", "found": true}')["desc"] == "齿轮图标"
    assert vision.extract_fields("no desc here")["desc"] == ""


def test_desc_verdict_flags_placeholder():
    ok, reason = vision.desc_verdict("元素简述", "设置")
    assert ok is False and "占位" in reason


def test_desc_verdict_flags_empty():
    ok, reason = vision.desc_verdict("", "设置")
    assert ok is False and "为空" in reason


def test_desc_verdict_accepts_real_description():
    assert vision.desc_verdict("设置标题文字，位于屏幕顶部", "设置") == (True, "")


def test_desc_verdict_marks_synonym_as_weak_signal():
    """模型说"齿轮图标"来指"设置"是合法的——只记可疑，不算硬证据。"""
    ok, reason = vision.desc_verdict("齿轮图标", "设置")
    assert ok is False and "无交集" in reason


def test_placeholder_desc_is_recorded_but_not_blocking(monkeypatch):
    monkeypatch.setenv("MAT_AI_METRICS", "0")   # 别往真实 HOME 写盘
    parsed = vision.parse_vision_response(
        {"found": True, "confidence": 0.95, "x": 0.5, "y": 0.25, "desc": "元素简述"},
        _png(), target="设置",
    )
    assert parsed is not None, "只观测不拦截：坐标照旧返回"
    r = vision.last_result()
    assert r["desc_ok"] is False
    assert "占位" in r["desc_reason"]


def test_real_desc_passes_audit(monkeypatch):
    monkeypatch.setenv("MAT_AI_METRICS", "0")
    vision.parse_vision_response(
        {"found": True, "confidence": 0.95, "x": 0.5, "y": 0.25, "desc": "设置标题文字"},
        _png(), target="设置",
    )
    r = vision.last_result()
    assert r["desc_ok"] is True and r["desc_reason"] == ""
