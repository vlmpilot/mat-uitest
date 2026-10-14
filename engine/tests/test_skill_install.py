"""`mat skill`（mobileapp-testing skill 的随包分发与安装）。

守的是 2026-10-11 修掉的那个断链：skill 此前只在仓库根目录、靠公开仓分发 ⇒
只 `pip install mat-uitest` 的用户机器上没有 `SKILL.md`，AI 助手"帮我装 skill"无料可装。
本文件既测安装动作，也测**打包**（文件真的在包内）——后者才是断链的根因。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from insta_engine import skill
from insta_engine.__main__ import cmd_skill

# -- 打包（断链根因：文件必须在包内，不能只在仓库里） --------------------------

def test_skill_is_bundled_in_package():
    directory = skill.bundled_skill_dir()
    assert directory.is_dir(), f"包内找不到 skill 目录：{directory}"
    names = [p.name for p in skill.bundled_files()]
    assert names == ["LICENSE.txt", "SKILL.md"]


def test_bundled_skill_md_has_expected_front_matter():
    text = (skill.bundled_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md 必须以 YAML front-matter 开头，否则助手不认"
    head = text.split("---", 2)[1]
    assert "name: mobileapp-testing" in head
    assert "description:" in head


# -- 安装动作 ----------------------------------------------------------------

def test_install_copies_files_byte_identical(tmp_path):
    result = skill.install(tmp_path)
    assert result["status"] == "installed"
    assert result["overwritten"] is False
    for path in skill.bundled_files():
        assert (tmp_path / skill.SKILL_NAME / path.name).read_bytes() == path.read_bytes()


def test_install_is_idempotent(tmp_path):
    skill.install(tmp_path)
    again = skill.install(tmp_path)
    assert again["status"] == "up-to-date"


def test_install_creates_missing_parent_dirs(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    assert skill.install(deep)["status"] == "installed"
    assert (deep / skill.SKILL_NAME / "SKILL.md").is_file()


def test_install_refuses_to_overwrite_user_edits_without_force(tmp_path):
    """用户可能把自己的团队约定补进 SKILL.md —— 静默覆盖会吃掉人家的修改。"""
    skill.install(tmp_path)
    edited = tmp_path / skill.SKILL_NAME / "SKILL.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n# 我的团队约定\n",
                      encoding="utf-8")
    result = skill.install(tmp_path)
    assert result["status"] == "differs"
    assert result["differing"] == ["SKILL.md"]
    assert "# 我的团队约定" in edited.read_text(encoding="utf-8"), "不带 --force 不该改动目标文件"


def test_install_force_overwrites(tmp_path):
    skill.install(tmp_path)
    edited = tmp_path / skill.SKILL_NAME / "SKILL.md"
    edited.write_text("被改坏了", encoding="utf-8")
    result = skill.install(tmp_path, force=True)
    assert result["status"] == "installed"
    assert result["overwritten"] is True
    assert "# Mobile App UI Testing with MAT" in edited.read_text(encoding="utf-8")


# -- 落点解析 ----------------------------------------------------------------

def test_resolve_target_known_names_and_dir_override(tmp_path):
    assert skill.resolve_target("claude", None) == Path("~/.claude/skills").expanduser()
    assert skill.resolve_target("workbuddy", None) == Path("~/.workbuddy/skills").expanduser()
    # --dir 优先于 --target；默认 claude
    assert skill.resolve_target("workbuddy", str(tmp_path)) == tmp_path
    assert skill.resolve_target(None, None) == Path("~/.claude/skills").expanduser()


def test_resolve_target_rejects_unknown_name_with_guidance():
    with pytest.raises(ValueError) as exc:
        skill.resolve_target("cursor", None)
    assert "cursor" in str(exc.value)
    assert "--dir" in str(exc.value)


# -- CLI 接线（用户真正敲的那条命令） ----------------------------------------

def _args(**kwargs):
    base = {"skill_cmd": "path", "target": None, "dir": None, "force": False}
    return SimpleNamespace(**{**base, **kwargs})


def test_cmd_skill_path_prints_bundled_dir(capsys):
    assert cmd_skill(_args(skill_cmd="path")) == 0
    out = capsys.readouterr().out
    assert str(skill.bundled_skill_dir()) in out
    assert "SKILL.md" in out


def test_cmd_skill_install_then_conflict_exit_codes(tmp_path, capsys):
    assert cmd_skill(_args(skill_cmd="install", dir=str(tmp_path))) == 0
    assert "已安装" in capsys.readouterr().out

    assert cmd_skill(_args(skill_cmd="install", dir=str(tmp_path))) == 0
    assert "已是最新" in capsys.readouterr().out

    (tmp_path / skill.SKILL_NAME / "SKILL.md").write_text("改了", encoding="utf-8")
    assert cmd_skill(_args(skill_cmd="install", dir=str(tmp_path))) == 1, "冲突必须以非 0 退出"
    out = capsys.readouterr().out
    assert "没有安装" in out and "SKILL.md" in out

    assert cmd_skill(_args(skill_cmd="install", dir=str(tmp_path), force=True)) == 0
    assert "已覆盖更新" in capsys.readouterr().out


def test_cmd_skill_install_rejects_unknown_target(capsys):
    assert cmd_skill(_args(skill_cmd="install", target="cursor")) == 1
    assert "--target" in capsys.readouterr().out
