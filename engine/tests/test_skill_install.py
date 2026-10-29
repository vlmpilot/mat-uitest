"""`mat skill`（mobileapp-testing skill 的随包分发与安装）。

守的是 2026-10-11 修掉的那个断链：skill 此前只在仓库根目录、靠公开仓分发 ⇒
只 `pip install mat-uitest` 的用户机器上没有 `SKILL.md`，AI 助手"帮我装 skill"无料可装。
本文件既测安装动作，也测**打包**（文件真的在包内）——后者才是断链的根因。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from insta_engine import __version__, skill
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


# 市场（WorkBuddy 开放平台 / 阿里云百炼等）对 frontmatter 有硬门槛：缺一个字段，
# 传上去 zip 就解析失败，而且**报错不会告诉你是哪个字段**。这里把门槛做成测试。
_MARKET_REQUIRED = ("name", "display_name", "display_name_en", "description",
                    "description_zh", "description_en", "version", "author")


def _front_matter() -> str:
    text = (skill.bundled_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    return text.split("---", 2)[1]


def test_front_matter_has_every_field_the_marketplace_requires():
    head = _front_matter()
    for key in _MARKET_REQUIRED:
        match = re.search(rf"^{key}:[ \t]*(.*)$", head, re.M)
        assert match, f"frontmatter 缺 {key}（市场上架时会解析失败）"
        assert match.group(1).strip(), f"frontmatter 的 {key} 是空值"


def test_front_matter_version_tracks_package_version():
    """市场清单与详情页显示的版本从 SKILL.md 注入；脱钩就会让用户装到"版本说不清"的技能。"""
    match = re.search(r'^version:[ \t]*"?([^"\n]+)"?[ \t]*$', _front_matter(), re.M)
    assert match, "frontmatter 缺 version"
    assert match.group(1).strip() == __version__, (
        f"SKILL.md 版本 {match.group(1).strip()} ≠ 包版本 {__version__}：发版时同步一下")


# -- 插件市场清单模板（mat-skill 仓由 scripts/sync_skill_repo.sh 渲染） ---------

_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "skill-marketplace"


def test_marketplace_templates_render_to_valid_manifests():
    if not _TEMPLATE_DIR.is_dir():
        pytest.skip("scripts/ 不在测试环境里（wheel 安装后没有它）")
    rendered = {}
    for name in ("marketplace.json", "plugin.json"):
        raw = (_TEMPLATE_DIR / name).read_text(encoding="utf-8")
        assert "__VERSION__" in raw, f"{name} 需要 __VERSION__ 占位，版本由同步脚本注入"
        rendered[name] = json.loads(raw.replace("__VERSION__", __version__))

    market = rendered["marketplace.json"]
    assert market["name"] and market["owner"]["name"], "marketplace.json 缺 name/owner"
    entry = market["plugins"][0]
    assert entry["name"] == skill.SKILL_NAME
    assert entry["source"] == "./", "插件根必须是仓库根（这样 skill 目录不需要复制一份）"
    assert entry["version"] == __version__

    plugin = rendered["plugin.json"]
    assert plugin["name"] == skill.SKILL_NAME
    assert plugin["skills"] == [f"./{skill.SKILL_NAME}"]



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


def test_targets_cover_the_domestic_agents_and_mark_unverified_ones():
    """国内助手各写各的目录：通用落点 ~/.agents/skills 是最大公约数，其余照官方文档列；
    只有出处不确定的才标「未实测」——标错会让人以为装完就生效。"""
    rows = {row["target"]: row for row in skill.targets_table()}
    assert rows["agents"]["path"] == str(Path("~/.agents/skills").expanduser())
    assert rows["trae"]["path"] == str(Path("~/.trae-cn/skills").expanduser())
    assert rows["qoderwork"]["path"] == str(Path("~/.qoderwork/skills").expanduser())
    assert rows["trae"]["verified"] is True
    assert rows["qwen"]["verified"] is False
    assert rows["codebuddy"]["verified"] is False
    # 每个落点都要有中文说明，否则 `mat skill targets` 输出里是空白
    assert all(row["label"] for row in rows.values())


# -- CLI 接线（用户真正敲的那条命令） ----------------------------------------

def _args(**kwargs):
    base = {"skill_cmd": "path", "target": None, "dir": None, "force": False}
    return SimpleNamespace(**{**base, **kwargs})


def test_cmd_skill_path_prints_bundled_dir(capsys):
    assert cmd_skill(_args(skill_cmd="path")) == 0
    out = capsys.readouterr().out
    assert str(skill.bundled_skill_dir()) in out
    assert "SKILL.md" in out


def test_cmd_skill_targets_lists_paths_and_recommends_agents(capsys):
    assert cmd_skill(_args(skill_cmd="targets")) == 0
    out = capsys.readouterr().out
    assert "agents" in out and ".agents/skills" in out
    assert "trae" in out and ".trae-cn/skills" in out
    assert "未实测" in out, "未实测的落点必须在同一屏里提示"


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
