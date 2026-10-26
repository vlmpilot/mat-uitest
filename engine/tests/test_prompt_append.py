"""追加段（prompts/<task>.append.md）+ 模板过期检测/升级（2026-10-25）。

背景：整模板复制到工作区后解析优先级让副本永远压过包内新版（模板冻结）。
追加式定制让用户只写增量；staleness/upgrade 让被冻结的副本有恢复路径。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine import promptstore
from insta_engine.ai_client import _assemble_local, _server_task_text
from insta_engine.web_api.prompts import api_prompts, api_prompts_save, api_prompts_upgrade

# -- 追加段读取 -----------------------------------------------------------------------

def test_read_append_missing_returns_none(tmp_path):
    assert promptstore.read_append("gen", tmp_path / "prompts") is None


def test_read_append_roundtrip_and_blank(tmp_path):
    ws = tmp_path / "prompts"
    ws.mkdir()
    (ws / "gen.append.md").write_text("生成步骤一律用中文 intent", encoding="utf-8")
    assert promptstore.read_append("gen", ws) == "生成步骤一律用中文 intent"
    (ws / "gen.append.md").write_text("   \n  ", encoding="utf-8")
    assert promptstore.read_append("gen", ws) is None, "空白追加段按「无」处理"


def test_assemble_local_appends(tmp_path, monkeypatch):
    """local 组装：追加段拼在模板尾部（占位符替换照常）。"""
    ws = tmp_path / "prompts"
    ws.mkdir()
    (ws / "gen.prompt").write_text("基础模板 {nl}", encoding="utf-8")
    (ws / "gen.append.md").write_text("补充规则:断言用 textContains", encoding="utf-8")
    monkeypatch.setattr(promptstore, "workspace_dir", lambda: ws)
    text = _assemble_local("gen", {"nl": "打开设置"})
    assert text.startswith("基础模板 打开设置")
    assert text.endswith("补充规则:断言用 textContains")


def test_server_task_text_carries_user_append(tmp_path, monkeypatch):
    """server 模式：追加段作为 user_append 字段发给服务端（与 repair 同款协议）。"""
    ws = tmp_path / "prompts"
    ws.mkdir()
    (ws / "gen.append.md").write_text("服务端也要拼", encoding="utf-8")
    monkeypatch.setattr(promptstore, "workspace_dir", lambda: ws)

    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": "{}"}

    import insta_engine.ai_client as ac
    monkeypatch.setattr(ac.requests, "post",
                        lambda url, **kw: captured.update(kw, url=url) or _Resp())

    class _Client:
        base_url = "http://fake"
        api_key = "k"
        prompt_mode = "server"

    _server_task_text(_Client(), "gen", {"nl": "x"}, "生成", images=None, rounds=None)
    assert captured["json"]["user_append"] == "服务端也要拼"


def test_server_task_text_omits_field_without_append(tmp_path, monkeypatch):
    """无追加段时**不带** user_append 键（请求体保持旧形状）。"""
    ws = tmp_path / "prompts"
    ws.mkdir()
    monkeypatch.setattr(promptstore, "workspace_dir", lambda: ws)

    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": "{}"}

    import insta_engine.ai_client as ac
    monkeypatch.setattr(ac.requests, "post",
                        lambda url, **kw: captured.update(kw, url=url) or _Resp())

    class _Client:
        base_url = "http://fake"
        api_key = "k"
        prompt_mode = "server"

    _server_task_text(_Client(), "gen", {"nl": "x"}, "生成", images=None, rounds=None)
    assert "user_append" not in captured["json"]


# -- 模板过期检测与升级 ------------------------------------------------------------------

def _make_ws(tmp_path: Path, ws_prompt: str | None = None) -> Path:
    ws = tmp_path / "prompts"
    ws.mkdir(exist_ok=True)
    if ws_prompt is not None:
        (ws / "gen.prompt").write_text(ws_prompt, encoding="utf-8")
    return ws


def test_staleness_detects_diverged_copy(tmp_path):
    """工作区副本与包内新版内容不同 ⇒ 报告过期。"""
    ws = _make_ws(tmp_path, ws_prompt="旧版模板内容（与包内 example 不同）")
    info = promptstore.staleness("gen", ws)
    assert info is not None and info["file"] == "gen.prompt"


def test_staleness_same_content_is_fine(tmp_path):
    """副本与包内一致（只是位置不同）不算过期。"""
    pkg_example = (promptstore.PKG_PROMPTS / "gen.prompt.example").read_text(encoding="utf-8")
    ws = _make_ws(tmp_path, ws_prompt=pkg_example)
    assert promptstore.staleness("gen", ws) is None


def test_staleness_package_active_not_reported(tmp_path):
    """解析命中包内模板（无工作区副本）不报告。"""
    ws = _make_ws(tmp_path, ws_prompt=None)
    assert promptstore.staleness("gen", ws) is None


def test_staleness_explicit_choice_not_reported(tmp_path):
    """choice 指名的文件不报告——用户明确要用它，升级反而破坏意图。"""
    ws = _make_ws(tmp_path, ws_prompt="我的定制模板")
    (ws / "gen-v2.prompt").write_text("显式选择的文件", encoding="utf-8")
    promptstore.write_choice(ws, "gen", "gen-v2.prompt")
    assert promptstore.staleness("gen", ws) is None


def test_api_upgrade_backups_and_falls_back(tmp_path):
    """升级 = 旧副本改名备份 + 解析回落包内 + choice 指名旧文件时清理。"""
    ws = _make_ws(tmp_path, ws_prompt="旧版内容")
    promptstore.write_choice(ws, "gen", "gen.prompt")
    code, body = api_prompts_upgrade(tmp_path, {"name": "gen"})
    assert code == 200 and body["ok"]
    bak = tmp_path / "prompts" / body["backup"]
    assert bak.exists() and bak.read_text(encoding="utf-8") == "旧版内容"
    # 解析回落包内模板
    active = promptstore.resolve("gen", ws)
    assert active.parent == promptstore.PKG_PROMPTS
    # choice 已清（指名的文件不存在了）
    assert not promptstore.read_choice(ws).get("gen")
    # 再升级:无可升级 ⇒ 400
    code, _ = api_prompts_upgrade(tmp_path, {"name": "gen"})
    assert code == 400


def test_api_slots_expose_stale_and_append(tmp_path):
    """槽位信息带 stale / append 状态（前端标签与入口的数据源）。"""
    _make_ws(tmp_path, ws_prompt="旧版内容")
    (tmp_path / "prompts" / "gen.append.md").write_text("我的追加", encoding="utf-8")
    code, body = api_prompts(tmp_path)
    assert code == 200
    gen = next(s for s in body["slots"] if s["name"] == "gen")
    assert gen["stale"] and gen["stale"]["file"] == "gen.prompt"
    assert gen["has_append"] is True and gen["append_file"] == "gen.append.md"


def test_api_save_allows_append_md(tmp_path):
    """控制台可保存 .append.md（追加段编辑入口走通用 save）。"""
    code, body = api_prompts_save(tmp_path, {"filename": "gen.append.md", "content": "追加内容"})
    assert code == 200 and (tmp_path / "prompts" / "gen.append.md").exists()
