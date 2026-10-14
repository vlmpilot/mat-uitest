"""`mat suite` 套件管理的回归测试。

为什么这些用例必须存在：`mat suite add x '*.json'` 会把**套件文件自己**匹配进来
（实测踩过：`check` 随后报 "DSL 不合法: cases/x.suite.json"）。这类"把自己算进去"
的 bug 肉眼很难发现，所以钉死。

CLI 错误路径也要钉：抛 traceback 给用户看是不可接受的（与 cmd_run 的既有原则同一）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine import suite_cmd


def _case(case_id: str, package: str = "com.demo.shop", name: str = "") -> dict:
    return {
        "caseId": case_id,
        "name": name or case_id,
        "target": {"package": package},
        "steps": [{"id": "s1", "intent": "启动", "action": "launch",
                   "params": {"package": package}}],
    }


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    (tmp_path / "cases").mkdir()
    for i in range(3):
        (tmp_path / "cases" / f"c{i}.json").write_text(
            json.dumps(_case(f"c{i}"), ensure_ascii=False), encoding="utf-8")
    return tmp_path


# ---- 读写与列举 ------------------------------------------------------------

def test_new_creates_suite_with_refs(ws: Path):
    assert suite_cmd.cmd_new(_Args(ws, "smoke", add=["c0.json", "c1.json"])) == 0
    data = suite_cmd.read_suite(ws / "cases" / "smoke.suite.json")
    assert data["name"] == "smoke"
    # 条目=纯文件名（配置台 api_suite_save 同构）；run 按「套件所在目录/条目」解析，
    # 纯文件名不依赖 CWD。历史 `cases/` 前缀由 _norm_ref 读时兼容。
    assert data["cases"] == ["c0.json", "c1.json"]


def test_new_refuses_empty_suite_by_default(ws: Path):
    """空套件跑不了（`load_case_or_suite` 明确要求 cases 非空）⇒ 不该被默认建出来。"""
    assert suite_cmd.cmd_new(_Args(ws, "empty")) == 1
    assert not (ws / "cases" / "empty.suite.json").exists()


def test_new_does_not_clobber_without_force(ws: Path):
    suite_cmd.cmd_new(_Args(ws, "smoke", add=["c0.json"]))
    assert suite_cmd.cmd_new(_Args(ws, "smoke", add=["c1.json"])) == 1
    assert suite_cmd.read_suite(ws / "cases" / "smoke.suite.json")["cases"] == ["c0.json"]


def test_new_rejects_path_separator_in_name(ws: Path):
    """`cases/a/b.suite.json` 会写到子目录去，而 `mat run` 按 cases/ 平铺找 ⇒ 拒绝。

    注意这里断言的是**抛 ValueError** 而不是返回 1：名字校验在写盘前，
    翻译成 CLI 文案是 `_suite_guarded` 的职责（`__main__.py`），子命令层保持抛。
    """
    with pytest.raises(ValueError, match="套件名非法"):
        suite_cmd.cmd_new(_Args(ws, "a/b", add=["c0.json"]))
    assert not (ws / "cases" / "a").exists()


def test_list_empty_workspace(ws: Path):
    assert suite_cmd.cmd_list(_Args(ws)) == 0


def test_list_shows_counts(ws: Path, capsys):
    suite_cmd.cmd_new(_Args(ws, "smoke", add=["c0.json", "c1.json"]))
    suite_cmd.cmd_list(_Args(ws))
    assert "smoke" in capsys.readouterr().out


# ---- 增删 ----------------------------------------------------------------

def test_add_is_idempotent(ws: Path, capsys):
    suite_cmd.cmd_new(_Args(ws, "smoke", add=["c0.json"]))
    assert suite_cmd.cmd_add(_Args(ws, "smoke", cases=["c0.json"])) == 0
    assert suite_cmd.read_suite(ws / "cases" / "smoke.suite.json")["cases"] == ["c0.json"]


def test_add_glob_pulls_in_all(ws: Path):
    suite_cmd.cmd_new(_Args(ws, "all", add=["c0.json"]))
    assert suite_cmd.cmd_add(_Args(ws, "all", cases=["*.json"])) == 0
    assert len(suite_cmd.read_suite(ws / "cases" / "all.suite.json")["cases"]) == 3


def test_add_glob_excludes_the_suite_itself(ws: Path):
    """回归：`add x '*.json'` 曾把 `x.suite.json` 一起收进来，check 随即报它 DSL 不合法。"""
    suite_cmd.cmd_new(_Args(ws, "smoke", add=["c0.json"]))
    suite_cmd.cmd_add(_Args(ws, "smoke", cases=["*.json"]))
    refs = suite_cmd.read_suite(ws / "cases" / "smoke.suite.json")["cases"]
    assert not any(r.endswith(suite_cmd.SUITE_SUFFIX) for r in refs)


def test_add_glob_matching_only_suites_is_rejected(ws: Path):
    suite_cmd.cmd_new(_Args(ws, "smoke", add=["c0.json"]))
    with pytest.raises(FileNotFoundError, match="套件"):
        suite_cmd.cmd_add(_Args(ws, "smoke", cases=["*.suite.json"]))


def test_add_unknown_file_raises_actionable_error(ws: Path):
    suite_cmd.cmd_new(_Args(ws, "smoke", add=["c0.json"]))
    with pytest.raises(FileNotFoundError, match="找不到用例"):
        suite_cmd.cmd_add(_Args(ws, "smoke", cases=["nope.json"]))


def test_rm_keeps_order(ws: Path):
    suite_cmd.cmd_new(_Args(ws, "s", add=["c0.json", "c1.json", "c2.json"]))
    assert suite_cmd.cmd_rm(_Args(ws, "s", cases=["c1.json"])) == 0
    assert suite_cmd.read_suite(ws / "cases" / "s.suite.json")["cases"] == [
        "c0.json", "c2.json"]


def test_rm_refuses_to_empty_the_suite(ws: Path):
    suite_cmd.cmd_new(_Args(ws, "s", add=["c0.json"]))
    assert suite_cmd.cmd_rm(_Args(ws, "s", cases=["c0.json"])) == 1
    assert (ws / "cases" / "s.suite.json").exists()


def test_rm_preserves_inline_cases(ws: Path):
    """内联用例是别人的写法，rm 只该动文件引用，不该把它们删掉。"""
    suite_cmd.write_suite(ws / "cases" / "s.suite.json",
                          {"name": "s", "cases": ["cases/c0.json", _case("inline")]})
    suite_cmd.cmd_rm(_Args(ws, "s", cases=["c0.json"]))
    data = suite_cmd.read_suite(ws / "cases" / "s.suite.json")
    assert data["cases"] == [_case("inline")]


# ---- 校验 ----------------------------------------------------------------

def test_check_passes_on_clean_suite(ws: Path):
    suite_cmd.cmd_new(_Args(ws, "s", add=["c0.json", "c1.json"]))
    assert suite_cmd.cmd_check(_Args(ws, "s")) == 0


def test_check_accepts_console_style_suite(ws: Path):
    """回归：配置台 api_suite_save 写的条目是**纯文件名**，check 必须认——
    否则双入口不同构，CLI 校验对配置台建的套件全量误报「文件不存在」。"""
    suite_cmd.write_suite(ws / "cases" / "s.suite.json",
                          {"name": "s", "cases": ["c0.json", "c1.json"]})
    assert suite_cmd.cmd_check(_Args(ws, "s")) == 0


def test_check_accepts_legacy_prefixed_entries(ws: Path):
    """历史文件里 `cases/x.json` 前缀条目读时归一化兼容（旧版 mat suite 写出的）。"""
    suite_cmd.write_suite(ws / "cases" / "s.suite.json",
                          {"name": "s", "cases": ["cases/c0.json"]})
    assert suite_cmd.cmd_check(_Args(ws, "s")) == 0


def test_check_flags_missing_file(ws: Path):
    suite_cmd.write_suite(ws / "cases" / "s.suite.json", {"name": "s", "cases": ["gone.json"]})
    assert suite_cmd.cmd_check(_Args(ws, "s")) == 1


def test_check_flags_invalid_dsl(ws: Path):
    (ws / "cases" / "bad.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    suite_cmd.write_suite(ws / "cases" / "s.suite.json", {"name": "s", "cases": ["bad.json"]})
    assert suite_cmd.cmd_check(_Args(ws, "s")) == 1


def test_check_flags_mixed_packages(ws: Path):
    """跨 App 混跑是最常见的手写错误：不会报错，只会在跑到一半失败 ⇒ 必须提前拦。"""
    (ws / "cases" / "other.json").write_text(
        json.dumps(_case("other", package="com.other.app"), ensure_ascii=False), encoding="utf-8")
    suite_cmd.write_suite(ws / "cases" / "s.suite.json",
                          {"name": "s", "cases": ["c0.json", "other.json"]})
    assert suite_cmd.cmd_check(_Args(ws, "s")) == 1


# ---- 三写者同构（套件条目口径）---------------------------------------------

def test_add_norms_prefixed_entries_on_rewrite(ws: Path):
    """含历史前缀条目的套件 add 后落盘应全为纯文件名（写盘即迁移）。"""
    suite_cmd.write_suite(ws / "cases" / "s.suite.json",
                          {"name": "s", "cases": ["cases/c0.json"]})
    suite_cmd.cmd_add(_Args(ws, "s", cases=["c1.json"]))
    assert suite_cmd.read_suite(ws / "cases" / "s.suite.json")["cases"] == [
        "c0.json", "c1.json"]


def test_add_rejects_cases_outside_cases_dir(ws: Path):
    """套件只管 cases/ 平铺层的用例（配置台同口径）；引用外部路径会让三写者口径分裂。"""
    (ws / "outside.json").write_text(json.dumps(_case("out")), encoding="utf-8")
    suite_cmd.cmd_new(_Args(ws, "s", add=["c0.json"]))
    with pytest.raises(FileNotFoundError, match="找不到用例"):
        suite_cmd.cmd_add(_Args(ws, "s", cases=["outside.json"]))


def test_entries_resolvable_by_run_manifest_semantics(ws: Path):
    """`mat run` 按「套件所在目录/条目」解析且不依赖 CWD——纯文件名必须能被吃下。"""
    suite_cmd.cmd_new(_Args(ws, "s", add=["c0.json", "c1.json"]))
    suite_dir = ws / "cases"
    for item in suite_cmd.read_suite(suite_dir / "s.suite.json")["cases"]:
        assert (suite_dir / item).exists(), f"{item} 无法按 manifest 语义解析"


# ---- 加载兼容（套件要真能被 mat run 吃下）----------------------------------

def test_suite_written_by_cmd_is_loadable_by_run(ws: Path):
    from insta_engine.suite import load_case_or_suite

    suite_cmd.cmd_new(_Args(ws, "s", add=["c0.json", "c1.json"]))
    kind, payload = load_case_or_suite(ws / "cases" / "s.suite.json")
    assert kind == "suite"
    assert payload["cases"] == ["c0.json", "c1.json"]


def test_suite_file_would_be_skipped_by_run_directory_batch(ws: Path):
    """回归：`mat run cases/ --app <pkg>` 靠 `target.package` 过滤；套件没有 target，
    所以放同目录不会被误当成用例。**别把套件改名成 .json 之外的东西绕过这条。**"""
    suite_cmd.cmd_new(_Args(ws, "s", add=["c0.json"]))
    data = json.loads((ws / "cases" / "s.suite.json").read_text(encoding="utf-8"))
    assert (data.get("target") or {}).get("package") != "com.demo.shop"


class _Args:
    """极简命名空间，替掉 argparse（只为让每个测试读起来是"动作"而不是"配置"）。"""

    def __init__(self, root: Path, name: str = "", cases: list[str] | None = None,
                 add: list[str] | None = None, force: bool = False, allow_empty: bool = False):
        self.root = str(root)
        self.name = name
        self.cases = cases or []
        self.add = add or []
        self.force = force
        self.allow_empty = allow_empty
