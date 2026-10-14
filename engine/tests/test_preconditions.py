"""前置条件：DSL 承载 + 运行期可见 + 无法置备者拒跑（`preconditions.py`）。

锁住的核心是**"假失败"不再发生**：一条声明了「位置权限尚未授予」的用例，
跑下去只会产出一条假的定位失败（实测：某资讯类客户端 16 条里 2 条永远红）。
默认拒跑，并给出可行动的出路。
"""

from __future__ import annotations

import json

import pytest

from insta_engine import preconditions


def _run(argv: list[str]) -> int:
    """跑 CLI 并返回退出码 —— `main()` 内部会 `sys.exit()`，所以必须接住。"""
    from insta_engine import __main__ as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code or 0


def _write_case(tmp_path, preconditions_list: list[str]):
    case = {
        "caseId": "case_pre",
        "name": "位置授权弹窗-点击同意",
        "target": {"package": "com.tencent.news"},
        "preconditions": preconditions_list,
        "steps": [{"id": "s1", "action": "launch", "params": {"package": "com.tencent.news"}}],
    }
    cases = tmp_path / "cases"
    cases.mkdir(exist_ok=True)
    path = cases / "pre.json"
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAT_HOME", str(tmp_path / "mat-home"))
    monkeypatch.delenv("INSTA_HOME", raising=False)
    return tmp_path


# -- 判定：哪些前置是"按构造无法置备"的 -----------------------------------------

def test_negative_permission_state_is_unprovisionable():
    """否定式权限态 ⇒ 无法置备（MAT 的动作集改不了系统权限）。"""
    assert preconditions.unprovisionable(["位置权限尚未授予"]) == ["位置权限尚未授予"]
    assert preconditions.unprovisionable(["相机权限未开启"]) == ["相机权限未开启"]
    assert preconditions.unprovisionable(["已关闭通知权限"]) == ["已关闭通知权限"]


def test_positive_preconditions_are_not_blocked():
    """🔴 **肯定式**前置是运行的前提，拦它会误伤全部用例 —— 这条边界是刻意的。

    「App 已安装」不满足的话根本跑不起来；「已完成首次隐私授权」是已成立的状态。
    """
    assert preconditions.unprovisionable(["App 已安装"]) == []
    assert preconditions.unprovisionable(["已完成首次隐私授权"]) == []
    assert preconditions.unprovisionable(["已进入首页"]) == []
    assert preconditions.unprovisionable(["已进入资讯详情页"]) == []


def test_unprovisionable_dedupes_and_keeps_order():
    out = preconditions.unprovisionable(["位置权限尚未授予", "已进入首页", "位置权限尚未授予"])
    assert out == ["位置权限尚未授予"]


# -- 三分类（2026-10-13）：环境前提 / 系统态 / 可置备页面状态 -----------------------
#
# 为什么要分类：三类**处置完全不同**（不拦 / 拒跑 / 展开成导航步骤）。
# 用实测出现过的**原话**做断言，不自己编例子。

def test_classify_uses_real_examples_from_the_xlsx_batch():
    """三类各取实测原话：xlsx 批次的 IR 里就是这么写的。"""
    # ① 环境前提
    assert preconditions.classify("App 已安装") == preconditions.ENV
    assert preconditions.classify("已完成首次隐私授权") == preconditions.ENV
    # ② 不可置备的系统态
    assert preconditions.classify("位置权限尚未授予") == preconditions.SYSTEM
    # ③ 可置备的页面状态
    assert preconditions.classify("已进入搜索结果页") == preconditions.PAGE
    assert preconditions.classify("已进入资讯详情页") == preconditions.PAGE
    assert preconditions.classify("存在至少 1 条历史搜索") == preconditions.PAGE


def test_classify_defaults_to_page_fail_loud():
    """⚠️ 认不出来的一律归 ③：**宁可吵，不可哑**。

    误判方向不对称：环境前提误判成 ③ 只是多一条提示（人一看就知道）；
    页面状态误判成 ① 会让闸门**沉默**、用例继续照红（这才是真正的损失）。
    """
    assert preconditions.classify("缓存已预热") == preconditions.PAGE


def test_provisionable_picks_only_page_states():
    """`provisionable` 是"该被展开成导航步骤"的那一类（③）。"""
    items = ["App 已安装", "已进入资讯详情页", "位置权限尚未授予", "已进入搜索结果页"]
    assert preconditions.provisionable(items) == ["已进入资讯详情页", "已进入搜索结果页"]


def test_classify_all_groups_and_dedupes():
    grouped = preconditions.classify_all(
        ["App 已安装", "已进入搜索页", "已进入搜索页", "位置权限尚未授予", ""])
    assert grouped[preconditions.ENV] == ["App 已安装"]
    assert grouped[preconditions.PAGE] == ["已进入搜索页"]
    assert grouped[preconditions.SYSTEM] == ["位置权限尚未授予"]


def test_page_state_marker_wins_over_env_marker():
    """`已进入` 是页面状态标记 ⇒ 优先归 ③，即使句子里还夹着环境词。

    实测场景：「App 已进入搜索结果页」这种写法两种词都有；
    归 ③ 才对（要展开导航），归 ① 会让闸门沉默。
    """
    assert preconditions.classify("App 已进入搜索结果页") == preconditions.PAGE


def test_random_event_is_its_own_class():
    """④ 随机事件（实测原话）：既不能置备也不能展开 ⇒ 单列一类，**不喂给展开器**。

    「本次冷启动命中开屏广告」这种前置条件，展开等于让模型去"导航到一个开屏广告"。
    """
    assert preconditions.classify("本次冷启动命中开屏广告") == preconditions.RANDOM
    assert preconditions.is_random_event("本次冷启动命中开屏广告") is True
    # 随机态**不算**可置备页面状态（所以不会被展开）
    assert preconditions.provisionable(["本次冷启动命中开屏广告"]) == []


def test_random_marker_requires_cooccurrence():
    """⚠️ 随机态的判据要**窄**：宽了会把本该展开的页面状态误判成随机态 ⇒ 闸门沉默。

    `本次测试前已登录` 里有「本次」但没有"发生类"动词 ⇒ 不是随机态，按环境前提处理。
    """
    assert preconditions.is_random_event("本次测试前已登录") is False
    assert preconditions.classify("本次测试前已登录") == preconditions.ENV


def test_refusal_message_is_actionable():
    """拒跑话术必须说清**为什么**并给出**两条出路**（否则用户只会觉得工具坏了）。"""
    msg = preconditions.refusal_message("位置授权弹窗-点击同意", ["位置权限尚未授予"])
    assert "位置权限尚未授予" in msg
    assert "--allow-unmet-preconditions" in msg          # 出路 ①
    assert "改写用例" in msg                              # 出路 ②


# -- 运行期：拒跑与放行 ---------------------------------------------------------

def test_run_refuses_and_does_not_touch_the_device(isolated, monkeypatch, capsys):
    """🔴 拒跑必须发生在**连设备之前** —— 否则会被"设备没连上"掩盖成另一种错。"""
    from insta_engine import __main__ as cli

    def boom(*_a, **_k):
        raise AssertionError("前置条件预检未通过时不该去连设备")

    monkeypatch.setattr(cli, "_connect_driver", boom)
    path = _write_case(isolated, ["位置权限尚未授予"])

    assert _run(["run", str(path)]) == 3

    err = capsys.readouterr().err
    assert "无法置备" in err and "位置权限尚未授予" in err


def test_allow_flag_bypasses_the_gate(isolated, monkeypatch, capsys):
    """阳性对照：加 `--allow-unmet-preconditions` ⇒ 不再拒跑（证明上一条测的不是空转）。

    放行后走到"连设备"，测试里没有设备 ⇒ 返回 1；关键是**不是 3**、也不再打拒跑语。
    """
    from insta_engine import __main__ as cli

    monkeypatch.setattr(cli, "_connect_driver", lambda *a, **k: None)
    path = _write_case(isolated, ["位置权限尚未授予"])

    code = _run(["run", str(path), "--allow-unmet-preconditions"])

    assert code == 1, f"应走到连设备那一步（返回 1），实为 {code}"
    assert "无法置备" not in capsys.readouterr().err


def test_satisfiable_preconditions_do_not_block(isolated, monkeypatch, capsys):
    """可满足的前置（页面前提）只**可见**，不拦 —— 拦了会误伤大量正常用例。"""
    from insta_engine import __main__ as cli

    monkeypatch.setattr(cli, "_connect_driver", lambda *a, **k: None)
    path = _write_case(isolated, ["已进入资讯详情页"])

    assert _run(["run", str(path)]) == 1          # 不返回 3
    assert "无法置备" not in capsys.readouterr().err


def test_preconditions_survive_dsl_roundtrip():
    """前置条件必须**在 DSL 里活得下来** —— 它此前就是在 DSL 边界被丢掉的。"""
    from insta_engine.dsl import TestCase, dump_dsl

    case = TestCase.model_validate({
        "caseId": "case_p", "name": "x", "preconditions": ["已进入资讯详情页"],
        "steps": [{"id": "s1", "action": "launch", "params": {"package": "com.x"}}],
    })
    assert case.preconditions == ["已进入资讯详情页"]
    assert json.loads(dump_dsl(case))["preconditions"] == ["已进入资讯详情页"]


def test_case_without_preconditions_still_valid():
    """旧用例没有这个字段 ⇒ 默认空列表，不破坏兼容。"""
    from insta_engine.dsl import TestCase

    case = TestCase.model_validate({
        "caseId": "case_old",
        "steps": [{"id": "s1", "action": "launch", "params": {"package": "com.x"}}],
    })
    assert case.preconditions == []
