"""按应用的测试说明（app_notes/<包名>.md）：存储 + prompt 注入。"""

from __future__ import annotations

import pytest

from insta_engine import appnotes
from insta_engine.web_api.apps import api_app_note_get, api_app_note_put
from insta_engine.web_api.polish import api_polish


def test_write_read_roundtrip(tmp_path):
    appnotes.write(tmp_path, "com.example.app", "黑名单:不要点支付")
    assert appnotes.read(tmp_path, "com.example.app") == "黑名单:不要点支付"
    assert (tmp_path / "app_notes" / "com.example.app.md").exists()


def test_write_empty_removes_file(tmp_path):
    appnotes.write(tmp_path, "com.example.app", "说明")
    appnotes.write(tmp_path, "com.example.app", "   ")
    assert not (tmp_path / "app_notes" / "com.example.app.md").exists()
    assert appnotes.read(tmp_path, "com.example.app") == ""


def test_read_missing_returns_empty(tmp_path):
    assert appnotes.read(tmp_path, "com.other.app") == ""


def test_for_prompt_prefix_and_truncate(tmp_path):
    assert appnotes.for_prompt(tmp_path, "com.example.app") is None  # 无说明 → None（不塞空串）
    appnotes.write(tmp_path, "com.example.app", "禁止真实下单")
    text = appnotes.for_prompt(tmp_path, "com.example.app")
    assert text is not None and text.startswith(appnotes.PREFIX)
    assert "禁止真实下单" in text
    # 超长截断：注入长度 ≤ 前缀 + 上限
    appnotes.write(tmp_path, "com.big.app", "x" * 5000)
    clipped = appnotes.for_prompt(tmp_path, "com.big.app")
    assert clipped is not None and len(clipped) <= len(appnotes.PREFIX) + appnotes.MAX_CHARS + 1


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "a b", ";rm"])
def test_rejects_unsafe_package(tmp_path, bad):
    with pytest.raises(ValueError):
        appnotes.write(tmp_path, bad, "x")


def test_api_roundtrip(tmp_path):
    code, body = api_app_note_put(tmp_path, {"package": "com.example.app", "notes": "白名单:可加购物车"})
    assert code == 200 and body["ok"]
    code, body = api_app_note_get(tmp_path, "com.example.app")
    assert code == 200 and body["notes"] == "白名单:可加购物车"
    # GET 无文件：空串而非 404（缺说明不是错误）
    code, body = api_app_note_get(tmp_path, "com.none.app")
    assert code == 200 and body["notes"] == ""
    # 非法包名 400
    code, _ = api_app_note_put(tmp_path, {"package": "../evil", "notes": "x"})
    assert code == 400


def test_polish_injects_user_notes(tmp_path, monkeypatch):
    """润色调用点把 app_notes 注入 app_info.user_notes——与 /api/gen 同一口径。"""
    appnotes.write(tmp_path, "com.example.app", "黑名单:不要点支付")
    seen: dict = {}

    class _Stub:
        @classmethod
        def from_env(cls):
            return cls()

        def polish(self, nl, app_info):
            seen["app_info"] = app_info
            return "润色后的描述"

    import insta_engine.ai_client as ai_client_mod
    monkeypatch.setattr(ai_client_mod, "GenClient", _Stub)
    code, body = api_polish(tmp_path, {"nl": "搜索蓝牙耳机", "package": "com.example.app"})
    assert code == 200 and body["text"] == "润色后的描述"
    notes = seen["app_info"].get("user_notes") or ""
    assert "黑名单:不要点支付" in notes and notes.startswith(appnotes.PREFIX)


def test_polish_without_note_no_field(tmp_path, monkeypatch):
    """无说明时不塞空 user_notes——别让模型看到空约束段。"""
    seen: dict = {}

    class _Stub:
        @classmethod
        def from_env(cls):
            return cls()

        def polish(self, nl, app_info):
            seen["app_info"] = app_info
            return "ok"

    import insta_engine.ai_client as ai_client_mod
    monkeypatch.setattr(ai_client_mod, "GenClient", _Stub)
    api_polish(tmp_path, {"nl": "描述", "package": "com.example.app"})
    assert "user_notes" not in seen["app_info"]
