"""DSL 模型与变量替换的单元测试（CI 无设备依赖部分）。"""

from insta_engine.dsl import Action, LocatorStrategy, TestCase
from insta_engine.runner import substitute_vars

CASE_JSON = """
{
  "caseId": "case_t1",
  "name": "demo 登录",
  "target": {"package": "com.insta.demo"},
  "vars": {"账号": "alice"},
  "steps": [
    {"id": "s1", "intent": "输入账号", "action": "input",
     "target": {"strategy": "res-id", "value": "com.insta.demo:id/username",
                "fallback": {"strategy": "text", "value": "账号"}},
     "params": {"text": "${账号}"}},
    {"id": "s2", "intent": "点击登录", "action": "tap",
     "target": {"strategy": "res-id", "value": "com.insta.demo:id/login_btn"},
     "assertions": [{"type": "text-contains", "target": {"strategy": "res-id", "value": "com.insta.demo:id/welcome_text"}, "expected": "alice"}]}
  ]
}
"""


def test_case_roundtrip_and_fallback_chain():
    case = TestCase.model_validate_json(CASE_JSON)
    assert case.steps[0].action == Action.INPUT
    fallback = case.steps[0].target.fallback
    assert fallback.strategy == LocatorStrategy.TEXT
    assert fallback.value == "账号"


def test_substitute_vars():
    assert substitute_vars("输入${账号}", {"账号": "alice"}) == "输入alice"
    assert substitute_vars("${未知}变量", {}) == "${未知}变量"
    assert substitute_vars("无变量", {}) == "无变量"
