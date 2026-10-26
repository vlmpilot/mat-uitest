"""P1-3：Maestro 导入/导出——映射表、warnings 不静默、CLI 往返。

flow fixture 仿官方文档示例（config 头 + `---` + 命令序列），
覆盖方案 §3.7.2 映射表全行 + 不支持命令的显式 warning。
"""

from __future__ import annotations

import argparse
import json

import pytest

from insta_engine.dsl import Action, AssertionType, LocatorStrategy, TestCase
from insta_engine.maestro_import import import_maestro, to_maestro

FLOW = """\
appId: com.example.shop
name: login
---
- launchApp
- tapOn: "我的"
- tapOn:
    id: "com.example.shop:id/btn_login"
- inputText: "user@example.com"
- assertVisible: "欢迎回来"
- assertNotVisible: "账号或密码错误"
- swipe: UP
- back
- runFlow: flows/share.yaml
- repeat:
    times: 3
    commands:
      - tapOn: "签到"
"""


@pytest.fixture()
def flow_file(tmp_path):
    p = tmp_path / "flows" / "login.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(FLOW, encoding="utf-8")
    return p


# -- 导入：映射表 ---------------------------------------------------------------

def test_import_maps_all_documented_verbs(flow_file):
    case, warnings = import_maestro(flow_file)
    acts = [s.action for s in case.steps]
    assert acts == [Action.LAUNCH, Action.TAP, Action.TAP, Action.INPUT,
                    Action.WAIT, Action.WAIT, Action.SWIPE, Action.BACK]
    launch, tap_text, tap_id, inp = case.steps[:4]
    assert "package" not in launch.params and case.target["package"] == "com.example.shop"
    assert tap_text.target.strategy == LocatorStrategy.TEXT
    assert tap_text.target.value == "我的"
    assert tap_id.target.strategy == LocatorStrategy.RES_ID
    assert inp.target is tap_id.target or (
        inp.target.strategy == tap_id.target.strategy
        and inp.target.value == tap_id.target.value), "inputText 借上一条 tap 的定位器"
    assert inp.params["text"] == "user@example.com"
    av, anv = case.steps[4], case.steps[5]
    assert av.assertions[0].type == AssertionType.EXISTS
    assert av.assertions[0].target.value == "欢迎回来"
    assert anv.assertions[0].type == AssertionType.NOT_EXISTS
    assert case.steps[6].params["direction"] == "up"
    assert case.meta["source"] == "maestro"


def test_unsupported_commands_become_warnings_not_silence(flow_file):
    case, warnings = import_maestro(flow_file)
    assert any("runFlow" in w for w in warnings), "runFlow 必须显式列出"
    assert any("repeat" in w for w in warnings), "repeat 必须显式列出"
    assert case.meta["warnings"] == warnings, "warnings 同步进 meta（报告可见）"


def test_inputtext_without_tap_warns_and_keeps_step(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("appId: com.x\n---\n- inputText: \"hi\"\n", encoding="utf-8")
    case, warnings = import_maestro(p)
    assert len(case.steps) == 1 and case.steps[0].target is None
    assert any("无前置 tapOn" in w for w in warnings)


def test_assert_visible_composite_prefers_id_with_note(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text('appId: com.x\n---\n- assertVisible:\n'
                 '    id: "com.x:id/ok"\n    text: "确定"\n', encoding="utf-8")
    case, warnings = import_maestro(p)
    a = case.steps[0].assertions[0]
    assert a.target.strategy == LocatorStrategy.RES_ID
    assert any("text" in n for n in case.meta["notes"]), "被丢弃的 text 条件要显式说明"


def test_scroll_until_visible_is_approximate(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text('appId: com.x\n---\n- scrollUntilVisible:\n'
                 '    element:\n      id: "com.x:id/footer"\n', encoding="utf-8")
    case, warnings = import_maestro(p)
    s = case.steps[0]
    assert s.action == Action.SCROLL
    assert s.assertions[0].target.value == "com.x:id/footer"
    assert any("近似" in n for n in case.meta["notes"])


def test_missing_pyyaml_gets_install_hint(tmp_path, monkeypatch):
    import builtins

    p = tmp_path / "f.yaml"
    p.write_text("appId: com.x\n---\n- back\n", encoding="utf-8")
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "yaml":
            raise ImportError("No module named 'yaml'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ValueError, match=r"mat-uitest\[maestro\]"):
        import_maestro(p)


def test_missing_app_id_rejected(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("- back\n", encoding="utf-8")
    with pytest.raises(ValueError, match="appId"):
        import_maestro(p)


# -- 反向导出（MAT → Maestro）---------------------------------------------------

def test_to_maestro_round_trip(flow_file, tmp_path):
    case, _ = import_maestro(flow_file)
    yaml_text = to_maestro(case)
    assert yaml_text.startswith("appId: com.example.shop\n---\n")
    assert "- tapOn: \"我的\"" in yaml_text
    assert '- tapOn: {id: "com.example.shop:id/btn_login"}' in yaml_text
    assert '- inputText: "user@example.com"' in yaml_text
    assert '- assertVisible: "欢迎回来"' in yaml_text
    assert "- assertNotVisible: \"账号或密码错误\"" in yaml_text
    assert "- swipe: UP" in yaml_text and "- back" in yaml_text
    # 往返一致性：导出的 yaml 再导入，核心步骤同构（≥80% 可执行口径）
    back = tmp_path / "rt.yaml"
    back.write_text(yaml_text, encoding="utf-8")
    case2, _ = import_maestro(back)
    # launch 由 appId 头承担（to_maestro 不生成 launchApp）⇒ 双侧都排除 launch/wait 再比。
    # 另：MAT 的 input 导出为 tapOn+inputText 两行 ⇒ 再导入多一个同字段 TAP——语义等价，
    # 所以断言用**子序列匹配**（允许 tap 插入），不要求逐项相等。
    acts1 = [s.action for s in case.steps if s.action not in (Action.WAIT, Action.LAUNCH)]
    acts2 = [s.action for s in case2.steps if s.action not in (Action.WAIT, Action.LAUNCH)]
    pos = 0
    for a in acts1:
        while pos < len(acts2) and acts2[pos] != a:
            pos += 1
        assert pos < len(acts2), f"{a} 在再导入序列中丢失（{acts1} vs {acts2}）"
        pos += 1


def test_to_maestro_untranslatable_step_becomes_comment(tmp_path):
    case = TestCase.model_validate({
        "caseId": "c", "name": "n", "target": {"package": "com.x"},
        "steps": [{"id": "s1", "intent": "坐标点击", "action": "tap",
                   "target": {"strategy": "point", "value": "120,320"}},
                  {"id": "s2", "intent": "接口断言", "action": "api",
                   "params": {"url": "http://a.b/c"}}],
    })
    text = to_maestro(case)
    assert "翻译不了" in text and "point" in text, "point 定位不静默丢弃"
    assert "无 Maestro 对应命令" in text and "api" in text


# -- CLI ----------------------------------------------------------------------

def test_cli_import_maestro_writes_case(flow_file, tmp_path, capsys, monkeypatch):
    from insta_engine.__main__ import cmd_import

    monkeypatch.chdir(tmp_path)
    out = tmp_path / "cases" / "login.json"
    rc = cmd_import(argparse.Namespace(file=str(flow_file), package=None, template="auto",
                                       limit=None, expand_preconditions=True,
                                       format="maestro", out=str(out)))
    assert rc == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["caseId"] == "login" and saved["meta"]["source"] == "maestro"
    assert "导入完成" in capsys.readouterr().out


def test_cli_export_maestro(flow_file, tmp_path, capsys, monkeypatch):
    from insta_engine.__main__ import cmd_export, cmd_import

    monkeypatch.chdir(tmp_path)
    case_file = tmp_path / "cases" / "login.json"
    cmd_import(argparse.Namespace(file=str(flow_file), package=None, template="auto",
                                  limit=None, expand_preconditions=True,
                                  format="maestro", out=str(case_file)))
    capsys.readouterr()
    rc = cmd_export(argparse.Namespace(case=str(case_file), format="maestro",
                                       out=str(tmp_path / "login.yaml"), media=False))
    assert rc == 0
    text = (tmp_path / "login.yaml").read_text(encoding="utf-8")
    assert text.startswith("appId: com.example.shop")
    assert "导入完成" not in capsys.readouterr().out
