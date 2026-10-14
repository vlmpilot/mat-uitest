"""Prompt 文件管理（2026-10-14，发起人：控制台里可选择使用的 prompt 文件）。

覆盖：选择生效（.choice.json 最高优先）/ 保存与恢复默认 / 越界文件名防护 /
工作区优先的内容读取 / doctor 来源标签。
"""

from pathlib import Path

import pytest

from insta_engine import promptstore
from insta_engine.web_api.prompts import (
    api_prompts,
    api_prompts_content,
    api_prompts_save,
    api_prompts_select,
)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "prompts").mkdir()
    return tmp_path


def test_api_prompts_lists_package_template_and_mode(root):
    out = api_prompts(root)[1]
    assert out["mode"] == "local" and out["server"] is False
    names = [s["name"] for s in out["slots"]]
    assert names == ["gen", "convert"]
    gen = out["slots"][0]
    files = [c["file"] for c in gen["candidates"]]
    assert "gen.prompt.example" in files  # 包内模板始终可见
    assert all(c["source"] == "package" for c in gen["candidates"])
    assert gen["active"]["file"] == "gen.prompt.example"  # 工作区为空时回落包内
    assert gen["selected"] is None


def test_save_then_select_changes_resolution_and_doctor_label(root):
    saved = api_prompts_save(root, {"filename": "my-gen.prompt", "content": "自定义 {var}"})
    assert saved[0] == 200 and saved[1]["existed"] is False
    # 保存 ≠ 生效：默认顺序下 gen.prompt 不存在，仍回落包内
    assert promptstore.resolve("gen", root / "prompts").name == "gen.prompt.example"

    code, payload = api_prompts_select(root, {"name": "gen", "file": "my-gen.prompt"})
    assert code == 200
    assert promptstore.resolve("gen", root / "prompts").name == "my-gen.prompt"
    assert promptstore.read("gen", root / "prompts") == "自定义 {var}"
    assert payload["slot"]["selected"] == "my-gen.prompt"
    active = [c for c in payload["slot"]["candidates"] if c["active"]]
    assert [c["file"] for c in active] == ["my-gen.prompt"]

    kind, path = promptstore.describe_resolution("gen", root / "prompts")
    assert kind == "chosen" and path is not None and path.name == "my-gen.prompt"

    # 恢复默认 → 回落包内
    code, _ = api_prompts_select(root, {"name": "gen", "file": None})
    assert code == 200
    assert promptstore.resolve("gen", root / "prompts").name == "gen.prompt.example"
    assert promptstore.describe_resolution("gen", root / "prompts")[0] == "package"


def test_slot_candidates_filtered_to_task_family(root):
    """下拉只列本槽位「同族」prompt（发起人 2026-10-14：gen 选了 convert/polish 等
    其他任务的模板只会生成坏结果）。同名与前后缀派生都算同族；显式选择的文件始终保留。"""
    for f in ("gen-v2.prompt", "my-gen.prompt", "polish.prompt", "convert.prompt"):
        (root / "prompts" / f).write_text("x", encoding="utf-8")
    gen = api_prompts(root)[1]["slots"][0]
    files = [c["file"] for c in gen["candidates"]]
    assert "gen-v2.prompt" in files and "my-gen.prompt" in files
    assert "polish.prompt" not in files and "convert.prompt" not in files
    conv = api_prompts(root)[1]["slots"][1]
    cfiles = [c["file"] for c in conv["candidates"]]
    assert "convert.prompt" in cfiles and "gen-v2.prompt" not in cfiles
    # 显式选择过的文件即使不合族规也保留（choice 里指名了）
    (root / "prompts" / "polish.prompt.example").write_text("y", encoding="utf-8")
    promptstore.write_choice(root / "prompts", "convert", "polish.prompt.example")
    conv2 = api_prompts(root)[1]["slots"][1]
    assert "polish.prompt.example" in [c["file"] for c in conv2["candidates"]]


def test_select_rejects_bad_names_and_missing_files(root):
    assert api_prompts_select(root, {"name": "gen", "file": "../x.prompt"})[0] == 400
    assert api_prompts_select(root, {"name": "gen", "file": "a/b.prompt"})[0] == 400
    assert api_prompts_select(root, {"name": "gen", "file": ".choice.json"})[0] == 400
    assert api_prompts_select(root, {"name": "gen", "file": "nope.prompt"})[0] == 404
    assert api_prompts_select(root, {"name": "diagnose", "file": None})[0] == 400  # 槽位白名单
    # 越界选择即便混进 choice 文件也不参与解析（存进去也读不出来）
    promptstore.write_choice(root / "prompts", "gen", "../secret.prompt")
    assert promptstore.describe_resolution("gen", root / "prompts")[0] == "package"


def test_save_rejects_bad_filename_and_empty_content(root):
    assert api_prompts_save(root, {"filename": "../x.prompt", "content": "hi"})[0] == 400
    assert api_prompts_save(root, {"filename": "sub/x.prompt", "content": "hi"})[0] == 400
    assert api_prompts_save(root, {"filename": "x.txt", "content": "hi"})[0] == 400
    assert api_prompts_save(root, {"filename": "x.prompt", "content": "   "})[0] == 400
    # 覆盖已存在是刻意的（编辑保存=原地更新）
    api_prompts_save(root, {"filename": "x.prompt", "content": "v1"})
    code, payload = api_prompts_save(root, {"filename": "x.prompt", "content": "v2"})
    assert code == 200 and payload["existed"] is True
    assert (root / "prompts" / "x.prompt").read_text(encoding="utf-8") == "v2"


def test_content_reads_workspace_first(root):
    (root / "prompts" / "gen.prompt.example").write_text("工作区版", encoding="utf-8")
    out = api_prompts_content(root, "gen.prompt.example")
    assert out[0] == 200 and out[1]["source"] == "workspace"
    assert "工作区版" in out[1]["content"]
    # 工作区没有的读包内
    out = api_prompts_content(root, "convert.prompt.example")
    assert out[0] == 200 and out[1]["source"] == "package"
    assert api_prompts_content(root, "../x.prompt")[0] == 400
    assert api_prompts_content(root, "ghost.prompt")[0] == 404


def test_workspace_env_unifies_console_and_cli(monkeypatch, tmp_path: Path):
    """控制台 serve() 注入 MAT_WORKSPACE 后，进程内任务（cwd=引擎目录）也解析工作区 prompts/。"""
    ws = tmp_path / "ws"
    (ws / "prompts").mkdir(parents=True)
    (ws / "prompts" / "gen.prompt").write_text("工作区自定义", encoding="utf-8")
    monkeypatch.setenv("MAT_WORKSPACE", str(ws))
    try:
        assert promptstore.workspace_dir() == ws / "prompts"
        assert promptstore.resolve("gen").name == "gen.prompt"
        assert promptstore.read("gen") == "工作区自定义"
    finally:
        monkeypatch.delenv("MAT_WORKSPACE")
