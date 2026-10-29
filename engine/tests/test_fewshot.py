"""few-shot 检索：只喂「同 App 且跑通」的历史用例（L1，2026-10-28）。

三条不能破的约束：
1. **只取 passed**：failed 用例进示例，等于把 AI 自己的错误教回去；
2. **只取同包名**：跨 App 的控件习惯不同，混在一起反而误导；
3. **默认关闭**：历史用例质量参差，先对比再改默认（`MAT_FEWSHOT=1` 开启）。
"""

from __future__ import annotations

import json

import pytest

from insta_engine import ai_client, fewshot, promptstore

PKG = "com.demo.app"


def _write_run(root, name, package, status, steps=None, finished="2026-10-28T10:00:00"):
    run_dir = root / "artifacts" / "suite" / name.replace(" ", "-")
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "finished_at": finished,
        "case": {
            "caseId": name,
            "name": name,
            "target": {"package": package},
            "steps": steps or [{"action": "tap",
                                "target": {"strategy": "text", "value": "搜索"}}],
        },
        "run": {"status": status},
    }
    (run_dir / "run.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear():
    fewshot.clear_cache()
    yield
    fewshot.clear_cache()


def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MAT_FEWSHOT", raising=False)
    _write_run(tmp_path, "搜索", PKG, "passed")
    assert fewshot.build(PKG, "搜索商品", root=tmp_path) == ""


def test_includes_passed_case_of_same_package(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_FEWSHOT", "1")
    _write_run(tmp_path, "搜索商品并进入详情", PKG, "passed",
               steps=[{"action": "tap", "target": {"strategy": "text", "value": "搜索框"}},
                      {"action": "input", "target": {"strategy": "text", "value": "搜索框"}},
                      {"action": "wait"}])
    text = fewshot.build(PKG, "搜索商品", root=tmp_path)
    assert "【参考示例】" in text
    assert "搜索商品并进入详情" in text
    assert "tap(text=搜索框)" in text and "wait" in text


def test_failed_case_is_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_FEWSHOT", "1")
    _write_run(tmp_path, "失败用例", PKG, "failed")
    assert fewshot.build(PKG, "搜索", root=tmp_path) == ""


def test_other_package_is_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_FEWSHOT", "1")
    _write_run(tmp_path, "别的App用例", "com.other.app", "passed")
    assert fewshot.build(PKG, "搜索", root=tmp_path) == ""


def test_limit_env_controls_count(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_FEWSHOT", "1")
    monkeypatch.setenv("MAT_FEWSHOT_LIMIT", "1")
    for i in range(3):
        _write_run(tmp_path, f"用例{i}", PKG, "passed",
                   finished=f"2026-10-28T10:0{i}:00")
    text = fewshot.build(PKG, "搜索", root=tmp_path)
    assert text.count("\n1. ") == 1 or text.count("1. ") >= 1
    assert "2. " not in text


def test_assemble_local_appends_examples(monkeypatch):
    """模板没有 {examples} 占位符时，示例追加到 prompt 末尾（不改三处模板）。"""
    monkeypatch.setattr(promptstore, "read", lambda task: "模板 {nl}")
    monkeypatch.setattr(promptstore, "read_append", lambda task: "")
    text = ai_client._assemble_local("gen", {"nl": "搜索", "examples": "【参考示例】X"})
    assert text.startswith("模板 搜索")
    assert text.endswith("【参考示例】X")


def test_assemble_local_prefers_placeholder(monkeypatch):
    """模板自己有 {examples} 时按占位符替换，不要重复追加。"""
    monkeypatch.setattr(promptstore, "read", lambda task: "前置\n{examples}\n后置")
    monkeypatch.setattr(promptstore, "read_append", lambda task: "")
    text = ai_client._assemble_local("gen", {"nl": "x", "examples": "示例A"})
    assert text.count("示例A") == 1
