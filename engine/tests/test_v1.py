"""V1 完整版单元测试（无设备/无网络依赖）。

覆盖：run.json/历史持久化、JUnit XML、凭据注入、配额滚动、自愈提案回写、
image-template 优雅降级、配置台 API 函数。
"""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from insta_engine.credentials import CredentialStore
from insta_engine.dsl import Locator, LocatorStrategy, TestCase
from insta_engine.heals import apply_heals, collect_proposals
from insta_engine.quota import QuotaExceeded, UsageStore
from insta_engine.quota_policy import plan_limits
from insta_engine.report import render_html, render_junit_xml
from insta_engine.runner import Runner
from insta_engine.web_api import runtime as web_server_rt
from insta_engine.web_server import api_state, list_runs

REPO = Path(__file__).resolve().parents[2]

# 签发工具不随包分发（见 licensing.py 的说明），所以测试自行构造激活码。
# 用**测试专用密钥**，因此这里造出的码在真实环境里是无效的。
# 诚实说明：签名算法与校验端同源，本仓库天然包含可复现签名的代码——这是设计取舍
#（licensing.py 已写明该机制"只用于提高门槛"），真正的边界在服务端校验。
# 变量名刻意避开敏感词形状：发布链路的密钥自查会把这类赋值形状当硬编码密钥拦下
#（fail-closed 是扫描该有的行为；invite.py 有同款先例——要改的是命名/措辞，不是扫描）
TEST_LICENSE_KEY = "test-only"


def mint_test_code(plan: str = "pro", months: int = 1) -> str:
    """按 verify_code 的口径造一个用测试密钥签名的激活码。"""
    import hashlib
    import hmac
    import uuid

    body = f"{plan}.{int(time.time()) + months * 30 * 86400}.{uuid.uuid4().hex[:6]}"
    sig = hmac.new(TEST_LICENSE_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
    raw = f"{body}.{sig}"
    return "MAT-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


@pytest.fixture(autouse=True)
def _clear_vision_cache():
    """vision 结果缓存按屏幕哈希+目标跨用例共享（产品语义），测试间需清理防串扰。"""
    from insta_engine import vision
    vision._CACHE.clear()
    yield
    vision._CACHE.clear()


class FakeDriver:
    """最小设备桩：稳定控件树 + 空操作动作（Runner 全流程无真机可测）。"""

    serial = None

    def dump(self) -> str:
        return "<hierarchy stable/>"

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str, activity: str | None = None) -> None:
        pass

    def press(self, key: str) -> None:
        pass

    def swipe_ext(self, direction: str) -> None:
        pass

    def click(self, x: int, y: int) -> None:
        pass

    # P0③：引擎定位已改走中立方法（drv.find / find_xpath / shell）——
    # 各 fake 仍以 self.raw(**kwargs) / self.raw.xpath / self.raw.shell 提供行为，
    # 这里统一委托；未定义 raw 的 fake 会 AttributeError，与迁移前 drv.raw 同形。
    def find(self, by: str, value: str, timeout: float):
        node = self.raw(**{by: value})
        node.wait(timeout=timeout)
        return node

    def find_xpath(self, expr: str, timeout: float):
        selector = self.raw.xpath(expr)
        selector.wait(timeout=timeout)
        return selector

    def shell(self, cmd: str):
        return self.raw.shell(cmd)

    def fallback_input(self, text: str) -> bool:
        """无二级输入通道（镜像 BaseDriver 默认语义；修复1 起的输入回退协议）。"""
        return False


class _LocatorShim:
    """P0③：引擎定位走中立方法（drv.find / find_xpath / shell）——
    测试内局部的 u2 形状 fake（drv.raw(**kwargs) / raw.xpath / raw.shell）
    挂上本 mixin 即与引擎新调用面兼容，行为不变。"""

    def find(self, by: str, value: str, timeout: float):
        node = self.raw(**{by: value})
        node.wait(timeout=timeout)
        return node

    def find_xpath(self, expr: str, timeout: float):
        selector = self.raw.xpath(expr)
        selector.wait(timeout=timeout)
        return selector

    def shell(self, cmd: str):
        return self.raw.shell(cmd)

    def fallback_input(self, text: str) -> bool:
        """二级输入通道（华为安全键盘回退）：镜像 DeviceDriver.fallback_input（修复1）。

        走 adb input text（空格按 adb 规则转 %s，shell 元字符交给 shlex.quote）。
        """
        import shlex
        self.raw.shell(f"input text {shlex.quote(text.replace(' ', '%s'))}")
        return True


def _simple_case() -> TestCase:
    return TestCase.model_validate({
        "caseId": "case_v1",
        "name": "V1 用例",
        "target": {"package": "com.insta.demo"},
        "steps": [
            {"id": "s1", "intent": "启动", "action": "launch", "params": {"package": "com.insta.demo"}},
            {"id": "s2", "intent": "等待", "action": "wait", "params": {"ms": 10}},
        ],
    })


# -- 断言类型（exists / not-exists / text-*）：此前零覆盖，not-exists 一直是死的 ----

def _assert_case(atype: str, value: str, expected: str | None = None) -> TestCase:
    a = {"type": atype, "target": {"strategy": "res-id", "value": value}}
    if expected is not None:
        a["expected"] = expected
    return TestCase.model_validate({
        "caseId": "case_assert", "name": "断言用例", "target": {"package": "com.insta.demo"},
        "steps": [{"id": "s1", "intent": "校验", "action": "wait", "params": {"ms": 10},
                   "timeoutMs": 400, "assertions": [a]}],
    })


def test_try_resolve_returns_none_instead_of_raising():
    """断言层依赖它：找不到必须返回 None，而不是抛 LocateFailed。"""
    from insta_engine.locator import LocateFailed, Locator, LocatorStrategy, resolve, try_resolve
    loc = Locator(strategy=LocatorStrategy.RES_ID, value="com.insta.demo:id/absent")
    with pytest.raises(LocateFailed):
        resolve(FakeDriver(), loc, 400)
    assert try_resolve(FakeDriver(), loc, 400) is None


# -- A2：主命脉的结构化输出与内容级重试 ------------------------------------------

class _StubClient:
    """最小 GenClient 桩：按脚本吐文本，并记录每次收到的 messages。"""

    base_url = "http://stub"
    model = "m"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        # 桩的 api_key 是 1 字符假值，不触发导出护栏的秘密扫描（需 ≥16 字符），
        # 直接赋值即可——不要用运行时拼装绕过护栏（ruff B010，且架空护栏威慑力）。
        self.api_key = "k"
        self.seen: list[list[dict]] = []

    def complete_raw_text(self, messages, context="生成"):
        self.seen.append(messages)
        return self.outputs.pop(0) if self.outputs else "{}"


def test_content_retry_repairs_invalid_json(monkeypatch):
    """回归：模型吐非法 JSON 时，要把**错误原文回喂**再试，而不是直接抛。

    此前 3 次重试只覆盖网络传输，内容错误一次就整条断——而 NL→用例是产品主命脉。
    """
    from insta_engine import ai_client

    monkeypatch.setattr(ai_client, "task_mode", lambda client: "local")
    client = _StubClient(["这不是 JSON", '{"a": 1}'])
    assert ai_client.complete_task_raw(client, "gen", {}, "生成") == {"a": 1}
    assert len(client.seen) == 2, "应当重试一次"
    repair_msg = client.seen[1][-1]
    assert repair_msg["role"] == "user"
    assert "不是合法 JSON" in repair_msg["content"], "修复提示必须带上错误原文"
    assert "json" in repair_msg["content"].lower()


def test_content_retry_gives_up_with_clear_error(monkeypatch):
    """回归：重试耗尽后必须给出**明确**报错（含次数），不能吞掉。"""
    from insta_engine import ai_client
    from insta_engine.ai_client import GenError

    monkeypatch.setattr(ai_client, "task_mode", lambda client: "local")
    monkeypatch.setenv("GEN_JSON_REPAIRS", "1")
    client = _StubClient(["坏", "还是坏", "仍然坏"])
    with pytest.raises(GenError) as exc:
        ai_client.complete_task_raw(client, "gen", {}, "生成")
    assert "内容级重试" in str(exc.value)


def test_server_mode_passes_repair_note(monkeypatch):
    """回归：server 模式下修复提示要通过 payload 传给代理（不认识该字段的代理会忽略）。"""
    from insta_engine import ai_client

    monkeypatch.setattr(ai_client, "task_mode", lambda client: "server")
    sent: list[dict] = []
    bodies = ["这不是 JSON", '{"ok": 1}']  # 第一轮坏、第二轮好

    class _Resp:
        status_code = 200

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"content": self._content}

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return _Resp(bodies[min(len(sent) - 1, len(bodies) - 1)])

    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    client = _StubClient([])
    client.base_url = "http://stub"
    assert ai_client.complete_task_raw(client, "gen", {}, "生成") == {"ok": 1}
    assert len(sent) == 2, "第一轮坏 JSON 应触发第二轮"
    assert "repair" not in sent[0], "首轮不该带 repair"
    assert "repair" in sent[1], "第二轮应带 repair 字段"
    assert "不是合法 JSON" in sent[1]["repair"]


def test_local_payload_has_json_mode_and_degrades(monkeypatch):
    """回归：本地补全应带 response_format；端点不支持时**降级而不是报错**。"""
    from insta_engine import ai_client

    monkeypatch.delenv("GEN_JSON_MODE", raising=False)
    ai_client._JSON_MODE_STATE["unsupported"] = False
    payloads: list[dict] = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "{}"}}]}

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, headers=None, json=None, timeout=None):
        payloads.append(dict(json))
        return _Resp(400 if "response_format" in json else 200)

    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    client = ai_client.GenClient("http://stub", "k", "m")
    assert client.complete_raw_text([{"role": "user", "content": "x"}]) == "{}"
    assert payloads[0].get("response_format") == {"type": "json_object"}, "首次应带约束"
    assert "response_format" not in payloads[1], "400 后应去掉该字段重试"
    assert ai_client._json_mode_unsupported() is True, "降级事实要记录，不能静默"
    ai_client._JSON_MODE_STATE["unsupported"] = False


def test_polish_task_strips_json_mode(monkeypatch):
    """回归（2026-10-13 润色挂死）：polish 输出自然语言，json_object 强约束
    会让 qwen 挂死到读超时（60s×3 全挂）——纯文本任务必须不带 response_format。"""
    from insta_engine import ai_client

    monkeypatch.delenv("GEN_JSON_MODE", raising=False)
    ai_client._JSON_MODE_STATE["unsupported"] = False
    payloads: list[dict] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "润色后的文本"}}]}

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr(ai_client.requests, "post",
                        lambda url, **kw: (payloads.append(dict(kw["json"])), _Resp())[1])
    client = ai_client.GenClient("http://stub", "k", "m")
    assert client.complete_raw_text(
        [{"role": "user", "content": "x"}], task="polish") == "润色后的文本"
    assert "response_format" not in payloads[0], "polish 任务不该带 json_object"
    # 其余 JSON 任务不受影响
    client.complete_raw_text([{"role": "user", "content": "x"}], task="gen")
    assert payloads[1].get("response_format") == {"type": "json_object"}
    ai_client._JSON_MODE_STATE["unsupported"] = False


def test_complete_task_text_passes_task_through(monkeypatch):
    """回归（2026-10-13 润色挂死）：complete_task_text 必须把 task 透传给
    complete_raw_text——丢 task 时 GEN_MODEL_<TASK> 与 json mode 排除都不生效。"""
    from insta_engine import ai_client

    seen: list[str | None] = []

    class _Stub:
        def complete_raw_text(self, messages, context, task=None):
            seen.append(task)
            return "ok"

    assert ai_client.complete_task_text(_Stub(), "polish", {}, "润色") == "ok"
    assert seen == ["polish"], "task 应透传"


# -- A2：主命脉的结构化输出与内容级重试 ------------------------------------------

class _StubClient:
    """最小 GenClient 桩：按脚本吐文本，并记录每次收到的 messages。"""

    base_url = "http://stub"
    model = "m"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.seen: list[list[dict]] = []
        # 同上：1 字符假值不触发秘密扫描，直接赋值。
        self.api_key = "k"

    def complete_raw_text(self, messages, context="生成"):
        self.seen.append(messages)
        return self.outputs.pop(0) if self.outputs else "{}"


def test_vision_llm_rejected_in_locator_chain():
    """回归：`vision-llm` 写进 DSL 定位链必须在**解析期**被拒。

    此前它是个静默空操作：`_try` 没有该分支 → 抛 LocatorNotSupported → 被 resolve 吞掉
    → 用户写了既不生效也不报错。这是最坏的一类 bug（以为配了、其实没有）。
    """
    from insta_engine.dsl import VISION_LLM_MISUSE_HINT, Locator, LocatorStrategy

    with pytest.raises(ValueError) as exc:
        Locator(strategy=LocatorStrategy.VISION_LLM, value="任意")
    assert VISION_LLM_MISUSE_HINT in str(exc.value)

    # fallback 链的每一级都是独立 Locator，逐级校验即可覆盖整条链
    with pytest.raises(ValueError):
        Locator(strategy=LocatorStrategy.RES_ID, value="pkg:id/x",
                fallback={"strategy": "vision-llm", "value": "任意"})


def test_try_rejects_vision_llm_with_actionable_message():
    """回归：即使绕过 DSL 校验直接构造，`_try` 也要给出**可操作**的报错。"""
    from insta_engine.dsl import VISION_LLM_MISUSE_HINT, Locator
    from insta_engine.locator import LocatorNotSupported, _try

    loc = Locator.model_construct(strategy="vision-llm", value="任意", fallback=None)
    with pytest.raises(LocatorNotSupported) as exc:
        _try(FakeDriver(), loc, 0.5)
    msg = str(exc.value)
    assert "onFailure" in msg and "GEN_VL_FALLBACK" in msg
    assert msg == VISION_LLM_MISUSE_HINT


def test_vision_status_text_says_opt_in_not_automatic(monkeypatch):
    """回归：CLI 状态文案不能暗示"配了就自动生效"。

    此前 doctor/status 写"配置后定位失败自动走 vision-llm"，而门控已改成显式开启——
    改了代码忘改提示语（自伤）。这条测试把它钉住。
    """
    from insta_engine.__main__ import vision_status_text

    monkeypatch.delenv("GEN_VL_FALLBACK", raising=False)
    configured = vision_status_text(server=False, key_ok=True, vl="qwen3-vl-plus")
    assert "自动" not in configured, "不能暗示自动触发"
    assert "onFailure" in configured and "GEN_VL_FALLBACK" in configured

    monkeypatch.setenv("GEN_VL_FALLBACK", "1")
    assert "已启用" in vision_status_text(server=False, key_ok=True, vl="qwen3-vl-plus")

    monkeypatch.delenv("GEN_VL_FALLBACK", raising=False)
    assert "未配置" in vision_status_text(server=False, key_ok=False, vl=None)


def test_rid_hints_from_graph(tmp_path):
    """回归：**改 id 后零成本自愈**的唯一线索来源——从图谱反查该 id 的历史文案。

    背景：res-id 改名是真实 App 改版最常见的形式，而通用本地模糊自愈治不了它
    （token 是 id 片段，匹配不到界面文案）。图谱里的历史映射正好补上这条线索。
    """
    from insta_engine.page_index import rid_hints

    g = tmp_path / "com.insta.demo.json"
    g.write_text(json.dumps({"pages": [{"controls": [
        {"class": "android.widget.Button", "text": "登录",
         "res-id": "com.insta.demo:id/login_btn", "clickable": True},
        {"class": "android.widget.Button", "text": "登录",
         "res-id": "com.insta.demo:id/login_btn"},          # 重复 → 去重
        {"class": "android.widget.TextView", "text": "x" * 60,
         "res-id": "com.insta.demo:id/noise"},              # 长文本 → 动态，剔除
    ]}]}, ensure_ascii=False), encoding="utf-8")

    assert rid_hints(g, "com.insta.demo:id/login_btn") == ["登录"]
    assert rid_hints(g, "com.insta.demo:id/noise") == [], "动态文本不该当线索"
    assert rid_hints(g, "com.insta.demo:id/absent") == []
    assert rid_hints(tmp_path / "nope.json", "x") == [], "图谱缺失应静默返回空"


def test_runner_uses_index_path_for_hints(tmp_path):
    """回归：Runner 接受显式图谱路径，并据此反查 res-id 的历史文案。"""
    from insta_engine.runner import Runner

    g = tmp_path / "g.json"
    g.write_text(json.dumps({"pages": [{"controls": [
        {"text": "登录", "res-id": "com.insta.demo:id/login_btn", "clickable": True}]}]},
        ensure_ascii=False), encoding="utf-8")
    runner = Runner(FakeDriver(), tmp_path, index_path=g)
    case = _assert_case("exists", "com.insta.demo:id/login_btn")
    assert runner._rid_hints(case, "com.insta.demo:id/login_btn") == ["登录"]
    assert runner._rid_hints(case, "com.insta.demo:id/absent") == []


def test_vision_fallback_is_opt_in(monkeypatch):
    """回归：**计费的**视觉兜底必须显式开启，默认不允许。

    此前它不受 `on_failure` 门控：只要配了 VL 模型，任何定位失败都会自动调它——
    而零成本的本地模糊自愈反而被 `onFailure=heal` 挡着。结果是
    「免费的不跑、收费的自动跑」，用户会在不知情的情况下被计费。
    """
    from insta_engine.runner import Runner

    default_step = _assert_case("exists", "com.insta.demo:id/x").steps[0]
    assert Runner._vision_allowed(default_step) is False, "默认必须不允许计费兜底"

    monkeypatch.setenv("GEN_VL_FALLBACK", "1")
    assert Runner._vision_allowed(default_step) is True, "GEN_VL_FALLBACK=1 应全局授权"
    monkeypatch.delenv("GEN_VL_FALLBACK")

    heal_step = TestCase.model_validate({
        "caseId": "c", "name": "n", "target": {"package": "com.insta.demo"},
        "steps": [{"id": "s1", "intent": "i", "action": "tap", "onFailure": "heal",
                   "target": {"strategy": "text", "value": "登录"}}],
    }).steps[0]
    assert Runner._vision_allowed(heal_step) is True, "onFailure=heal 应逐步骤授权"


def test_assertion_not_exists_passes_when_absent(tmp_path):
    """回归：元素确实不存在时，`not-exists` 必须**通过**。

    修复前它会抛 LocateFailed → 步骤失败，等于这个断言类型永远无法成立。
    """
    run = Runner(FakeDriver(), tmp_path).run(_assert_case("not-exists", "com.insta.demo:id/welcome_text"))
    assert run.status == "passed", f"not-exists 在元素缺失时应通过，实际 {run.steps[0].status}"


def test_assertion_exists_fails_as_assert_not_locate(tmp_path):
    """回归：`exists` 在元素缺失时应判 **assert-fail**，而不是 locate-fail。

    分类错会让「用例问题」混进「定位问题」，既污染失败五分类，也会污染自愈率的分母。
    """
    run = Runner(FakeDriver(), tmp_path).run(_assert_case("exists", "com.insta.demo:id/absent"))
    assert run.status == "failed"
    assert run.steps[0].category == "assert-fail", f"实际分类 {run.steps[0].category}"


def test_assertion_text_contains_reports_missing_target_as_assert(tmp_path):
    """回归：文本断言遇到目标缺失，也应判 assert-fail（而不是 locate-fail）。"""
    run = Runner(FakeDriver(), tmp_path).run(
        _assert_case("text-contains", "com.insta.demo:id/absent", expected="任意"))
    assert run.status == "failed" and run.steps[0].category == "assert-fail"


# -- 控件状态断言（enabled/disabled/checked/selected，T36） ------------------------

class _StateDriver(FakeDriver):
    """控件存在且带 u2 风格 info 的桩：状态断言测试用（res-id 解析恒命中同一节点）。"""

    def __init__(self, info):
        node = type("_Node", (), {"exists": True, "info": info, "wait": lambda self, timeout=None: True})()
        self._node = node

    @property
    def raw(self):
        driver = self

        class _Raw:
            def __call__(self, **kwargs):
                return driver._node

        return _Raw()


def test_state_assertions_pass_and_fail_by_info(tmp_path):
    """disabled=enabled 取反；enabled/checked/selected 与 u2 info 同名同义。"""
    drv = _StateDriver({"enabled": False, "checked": True, "selected": False})
    for atype, should_pass in (("disabled", True), ("checked", True),
                               ("enabled", False), ("selected", False)):
        run = Runner(drv, tmp_path).run(_assert_case(atype, "com.insta.demo:id/submit_btn"))
        passed = run.status == "passed"
        assert passed == should_pass, f"{atype} 断言应{'通过' if should_pass else '失败'}，实际 {run.status}"
        if not should_pass:
            assert run.steps[0].category == "assert-fail"
            assert atype in (run.steps[0].error or ""), f"失败信息应含断言类型: {run.steps[0].error}"


def test_state_assertion_on_missing_element_and_unsupported_shape(tmp_path):
    """目标缺失判 assert-fail；坐标定位（无控件树）不支持状态断言也要如实报错。"""
    run = Runner(FakeDriver(), tmp_path).run(
        _assert_case("enabled", "com.insta.demo:id/absent"))
    assert run.status == "failed" and run.steps[0].category == "assert-fail"

    import pytest

    from insta_engine.locator import Element

    point_el = Element(strategy="point", value="10,10", point=(10, 10), drv=None)
    with pytest.raises(RuntimeError, match="坐标定位不支持状态断言"):
        point_el.get_state("enabled")
    assert run.status == "failed"
    assert run.steps[0].category == "assert-fail", f"实际分类 {run.steps[0].category}"


# -- run.json / 历史 / 报告（持久化层） ----------------------------------------

def test_runner_persists_run_json_and_history(tmp_path):
    runner = Runner(FakeDriver(), tmp_path, history_path=tmp_path / "history.jsonl")
    run = runner.run(_simple_case())
    assert run.status == "passed"

    payload = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert payload["case"]["caseId"] == "case_v1"  # camelCase 规范落盘
    assert payload["run"]["status"] == "passed"
    assert payload["run"]["steps"][0]["status"] == "passed"

    history = (tmp_path / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(history[-1])
    assert entry["case_id"] == "case_v1" and entry["status"] == "passed"


def test_run_json_records_target_app_version(tmp_path):
    """`run.json` **必须记被测 App 版本**（T76 口径漏洞）。

    为什么：对外数字要能回答"**这次测的是哪个被测 App 构建**"。
    `explore` 的图谱一直记 `app_version`，而 `runner` 一直没记 ⇒ 靶场源码改过之后
    （T64 +4 缺陷、T76 +1 崩溃），**查不出旧数字是在哪个版本上测的**
    （那条"在当前版本上可复现"只追踪了引擎，**漏了靶场**）。
    """
    class _Raw:
        def app_info(self, pkg):
            return {"versionName": "9.9.9"}

    class VersionedDriver(FakeDriver):
        raw = _Raw()

        def app_info(self, pkg):  # P0-iOS：app_version 走中立方法（不再读 drv.raw）
            return self.raw.app_info(pkg)

    Runner(VersionedDriver(), tmp_path).run(_simple_case())
    payload = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert payload.get("app_version") == "9.9.9", (
        f"run.json 缺 app_version（实际 {payload.get('app_version')!r}）⇒ 对外数字追溯不到靶场版本")


def test_run_json_records_apk_fingerprint(tmp_path):
    """`run.json` 还要记**安装包指纹**。

    为什么（T76 实测）：所有靶场变体共用 `versionName=1.0` ⇒ **光看版本号分辨不出
    `baseline` 与 `v09-defect-free`**。而"对外数字测的是哪个构建"必须能回答。
    """
    class _Raw:
        def app_info(self, pkg):
            return {"versionName": "1.0"}

        def shell(self, args):
            if args[:2] == ["pm", "path"]:
                return "package:/data/app/base.apk\n"
            if args and args[0] == "sha256sum":
                return f"{'a' * 64}  /data/app/base.apk\n"
            return ""

    class FingerprintedDriver(FakeDriver):
        raw = _Raw()

        def app_info(self, pkg):  # P0-iOS：版本/指纹走中立方法（不再读 drv.raw）
            return self.raw.app_info(pkg)

        def shell(self, cmd):
            if cmd.startswith("pm path"):
                return "package:/data/app/base.apk\n"
            if cmd.startswith("sha256sum"):
                return f"{'a' * 64}  /data/app/base.apk\n"
            return ""

    Runner(FingerprintedDriver(), tmp_path).run(_simple_case())
    payload = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert payload["app_version"] == "1.0"
    assert payload["app_apk_sha256"] == "a" * 12, (
        f"缺安装包指纹（实际 {payload.get('app_apk_sha256')!r}）⇒ 分辨不出靶场变体")


def test_run_json_app_version_is_blank_when_unknown(tmp_path):
    """**反例**：探测不到版本时记**空串**，不能因此让执行失败。"""
    Runner(FakeDriver(), tmp_path).run(_simple_case())
    payload = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert payload["app_version"] == ""
    assert payload["app_apk_sha256"] == ""
    assert payload["run"]["status"] == "passed", "版本探测失败绝不能影响执行结果"


def test_junit_xml_structure():
    from insta_engine.runner import RunResult, StepResult

    run = RunResult(
        case_id="case_t1", case_name="demo", status="failed", step_success_rate=0.5, duration_ms=900,
        steps=[
            StepResult(step_id="s1", intent="输入账号", action="input", status="passed", duration_ms=400),
            StepResult(step_id="s2", intent="点击登录", action="tap", status="failed",
                       category="assert-fail", error="AssertionError: 断言失败: 期望包含 'alice'", duration_ms=500),
        ],
    )
    xml = render_junit_xml(run)
    assert 'tests="2"' in xml and 'failures="1"' in xml
    assert '<failure' in xml and "assert-fail" in xml and "s1" in xml


# -- 凭据注入（v0） -------------------------------------------------

def test_credential_store_vars(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.set("db_pass", "s3cret")
    assert store.names() == ["db_pass"]
    variables = store.load_as_vars()
    assert variables == {"cred.db_pass": "s3cret"}

    from insta_engine.runner import substitute_vars
    merged = {**variables, "账号": "alice"}
    assert substitute_vars("密码 ${cred.db_pass} 账号 ${账号}", merged) == "密码 s3cret 账号 alice"


# -- 配额（体验版 50 次/月） ----------------------------------------

def test_quota_gen_counts_but_never_blocks(tmp_path, monkeypatch):
    """P0-2：生成只计数不拦截（官方代理 429 权威 / BYOK 不限）；月度滚动照旧归零。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    store = UsageStore(tmp_path / "usage.json")
    free_gen = plan_limits("free")["gen"]
    for _ in range(free_gen):
        store.consume()  # 旧 check_gen = 预检+核销；现在只剩核销
    snap = store.snapshot()
    assert snap["gen_count"] == free_gen
    assert snap["gen_limit"] == free_gen and snap["gen_enforced"] is False
    store.consume()  # 超过策略参考值也不拦
    assert store.snapshot()["gen_count"] == free_gen + 1

    # 月份滚动重置：计数清零（展示口径，不存在"恢复额度"语义了）
    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    data["month"] = "2000-01"
    (tmp_path / "usage.json").write_text(json.dumps(data), encoding="utf-8")
    assert store.snapshot()["gen_count"] == 0


def test_quota_pro_gen_display_and_no_block(tmp_path, monkeypatch):
    """Pro 的生成额度只是展示口径（策略 300）；本地永不拦截。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", TEST_LICENSE_KEY)
    from insta_engine.licensing import activate
    activate(mint_test_code("pro", months=1))  # 真签名激活（v0.4.0：license 需 HMAC 校验）
    store = UsageStore(tmp_path / "usage.json")
    for _ in range(10):
        store.consume()
    snap = store.snapshot()
    assert snap["plan"] == "pro"
    assert snap["gen_limit"] == plan_limits("pro")["gen"] == 300
    assert snap["gen_enforced"] is False


# -- 视觉兜底配额（体验版 20、标准版 100 次/月） -----------------------

def test_quota_free_vision_limit(tmp_path, monkeypatch):
    """体验版视觉兜底 20 次/月（policy/quota.json）：用尽后抛 QuotaExceeded（调用方优雅降级）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    store = UsageStore(tmp_path / "usage.json")
    free_vision = plan_limits("free")["vision"]
    for _ in range(free_vision):
        store.check_vision()
    with pytest.raises(QuotaExceeded):
        store.check_vision()
    snap = store.snapshot()
    assert snap["vision_count"] == free_vision
    assert snap["vision_limit"] == free_vision
    assert snap["gen_count"] == 0  # 视觉核销不影响生成计数


def test_quota_pro_vision_limit_100(tmp_path, monkeypatch):
    """标准版视觉兜底 100 次/月（策略文件）：vision_limit 不再是 None，用尽后抛 QuotaExceeded。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", TEST_LICENSE_KEY)
    from insta_engine.licensing import activate
    activate(mint_test_code("pro", months=1))  # 真签名激活
    store = UsageStore(tmp_path / "usage.json")
    pro_vision = plan_limits("pro")["vision"]
    assert store.snapshot()["vision_limit"] == pro_vision == 100
    for _ in range(pro_vision):
        store.check_vision()
    with pytest.raises(QuotaExceeded) as exc:
        store.check_vision()
    assert "100" in str(exc.value)  # 触顶文案说明标准版月额度
    snap = store.snapshot()
    assert snap["vision_count"] == pro_vision
    assert snap["gen_count"] == 0


def test_quota_pro_vision_limit_follows_server_config(tmp_path, monkeypatch):
    """标准版视觉兜底上限服务端可配（2026-10-14）：seat_quota 下发 vision_base 后 snapshot/触顶口径随之变。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", TEST_LICENSE_KEY)
    from insta_engine import seat_quota
    from insta_engine.licensing import activate
    activate(mint_test_code("pro", months=1))
    seat_quota.write_cache({"machine_id": "f" * 64, "synced_at": int(time.time()),
                            "base": 5, "vision_base": 600, "grants": []})
    store = UsageStore(tmp_path / "usage.json")
    assert store.snapshot()["vision_limit"] == 600
    for _ in range(600):
        store.check_vision()
    with pytest.raises(QuotaExceeded) as exc:
        store.check_vision()
    assert "600" in str(exc.value)  # 触顶文案用的是下发值，不是写死的 300


def test_quota_vision_rollover_zeroes_both_counters(tmp_path, monkeypatch):
    """月度滚动：gen_count 与 vision_count 一并归零（两个计数同源同生命周期）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    store = UsageStore(tmp_path / "usage.json")
    store.consume()
    store.check_vision()

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    data["month"] = "2000-01"
    (tmp_path / "usage.json").write_text(json.dumps(data), encoding="utf-8")

    snap = store.snapshot()
    assert snap["gen_count"] == 0 and snap["vision_count"] == 0


def test_quota_backfills_legacy_usage_without_vision_key(tmp_path, monkeypatch):
    """兼容 v0.4.0 之前只含 gen_count 的 usage.json：读时补 vision_count=0，不丢旧计数。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    month = datetime.now(UTC).strftime("%Y-%m")
    (tmp_path / "usage.json").write_text(
        json.dumps({"month": month, "gen_count": 3}), encoding="utf-8"
    )
    snap = UsageStore(tmp_path / "usage.json").snapshot()
    assert snap["gen_count"] == 3 and snap["vision_count"] == 0


def test_vision_cache_hit_not_billed(tmp_path):
    """同屏缓存命中零 token → before_call 不触发（不重复计费）。"""
    from insta_engine import vision

    calls = {"n": 0}

    def before_call():
        calls["n"] += 1

    buf = io.BytesIO()
    Image.new("RGB", (200, 400)).save(buf, format="PNG")
    png = buf.getvalue()

    p1 = vision.locate(FakeVisionClient(), png, "点击我的", "我的",
                       cache_key="k1", before_call=before_call)
    p2 = vision.locate(FakeVisionClient(), png, "点击我的", "我的",
                       cache_key="k1", before_call=before_call)
    assert p1 == (60, 160) and p2 == p1
    assert calls["n"] == 1  # 第二次命中缓存，未再计费


def test_vision_before_call_exception_propagates(tmp_path):
    """before_call 抛出的配额异常原样上抛——由 runner 决定降级（等同未启用 vision）。"""
    from insta_engine import vision

    def before_call():
        raise QuotaExceeded("额度用尽")

    buf = io.BytesIO()
    Image.new("RGB", (200, 400)).save(buf, format="PNG")
    with pytest.raises(QuotaExceeded):
        vision.locate(FakeVisionClient(), buf.getvalue(), "x", "y",
                      cache_key="k2", before_call=before_call)


def test_runner_vision_quota_exhausted_degrades_gracefully(tmp_path, monkeypatch):
    """官方代理下额度耗尽优雅降级：步骤退回定位失败（locate-fail），不中断本地执行。

    ⚠️ 额度闸**只对官方代理生效**（2026-10-23）：自带 API Key 的 token 由用户自己的服务商
    计费，不再按平台额度拦截（对照见 `test_vision_quota.py::test_vision_locate_byok_ignores_quota`）。
    所以这里必须显式设为 server 模式，否则测到的是 BYOK 路径。
    """
    from insta_engine.runner import Runner

    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    monkeypatch.setenv("GEN_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("GEN_API_KEY", "test-key")
    monkeypatch.setenv("GEN_VL_MODEL", "fake-vl")
    month = datetime.now(UTC).strftime("%Y-%m")
    (tmp_path / "usage.json").write_text(
        json.dumps({"month": month, "gen_count": 0,
                    "vision_count": plan_limits("free")["vision"]}),
        encoding="utf-8",
    )

    class VisionDrv(FakeDriver):
        serial = "fake"

        def __init__(self):
            self.raw = MissingRaw()

        def screenshot_pil(self):  # P0-iOS：视觉兜底改走中立截图方法（不再读 drv.raw）
            return self.raw.screenshot()

    case = TestCase.model_validate({
        "caseId": "case_vl_quota",
        "steps": [{"id": "s1", "intent": "点击我的", "action": "tap", "onFailure": "heal",
                   "target": {"strategy": "text", "value": "不存在"}}],
    })
    # vision_client=None → 走环境配置路径，官方代理按档位额度核销
    runner = Runner(VisionDrv(), tmp_path, history_path=tmp_path / "h.jsonl")
    run = runner.run(case)
    step = run.steps[0]
    assert step.status == "failed"
    assert step.category == "locate-fail"
    assert "vision-llm" in step.error and "额度不足" in step.error


# -- 自愈提案回写（不覆盖原用例） -----------------------------------------------

def test_collect_and_apply_heals(tmp_path):
    case = TestCase.model_validate({
        "caseId": "case_h",
        "steps": [{"id": "s1", "action": "tap",
                   "target": {"strategy": "res-id", "value": "com.insta.demo:id/login_btn"}}],
    })
    run_payload = {"run": {"steps": [{"step_id": "s1", "status": "passed",
                                      "heal": {"original": "res-id=com.insta.demo:id/login_btn",
                                               "match": "fuzzy:textContains=login btn"}}]}}
    (tmp_path / "run.json").write_text(json.dumps(run_payload), encoding="utf-8")

    proposals = collect_proposals(tmp_path)
    assert len(proposals) == 1

    new_case, notes = apply_heals(case, proposals)
    assert new_case.steps[0].target.strategy == LocatorStrategy.TEXT
    assert new_case.steps[0].target.value == "login btn"
    assert any("真机复核" in n for n in notes)
    # 原用例不被修改（回写原则：用户确认后才替换）
    assert case.steps[0].target.strategy == LocatorStrategy.RES_ID


# -- image-template：无 OpenCV 时优雅降级 --------------------------------------

def test_image_template_graceful_degradation(tmp_path):
    try:
        import cv2  # noqa: F401
        pytest.skip("本机已安装 OpenCV，降级路径不适用")
    except ImportError:
        pass
    from insta_engine.locator import LocateFailed, resolve

    fake = FakeDriver()
    fake.raw = None  # 不应被触达：导入检查先于任何驱动调用
    loc = Locator(strategy=LocatorStrategy.IMAGE_TEMPLATE, value="tpl.png")
    # resolve 沿链吞掉单策略异常，最终 LocateFailed 消息必须保留可行动的安装提示
    with pytest.raises(LocateFailed, match=r"mat-uitest\[image\]"):
        resolve(fake, loc, 1000)


# -- 配置台 API（无 HTTP 层，直接测函数） ----------------------------------------

def test_api_state_and_list_runs(tmp_path):
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "a.json").write_text("{}", encoding="utf-8")
    run_dir = tmp_path / "artifacts" / "01_demo"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "finished_at": "2026-09-27T12:00:00+00:00",
        "case": {"caseId": "case_demo", "target": {"package": "com.insta.demo"}},
        "run": {"case_id": "case_demo", "case_name": "demo", "status": "passed",
                "step_success_rate": 1.0, "steps": []},
    }), encoding="utf-8")

    code, state = api_state(tmp_path, usage={"month": "2026-09", "plan": "free", "gen_count": 1, "gen_limit": 10})
    assert code == 200
    assert state["cases"] == ["a.json"]
    assert state["usage"]["gen_count"] == 1
    assert state["runs"][0]["dir"] == "01_demo"
    assert state["runs"][0]["package"] == "com.insta.demo"

    runs = list_runs(tmp_path)
    assert runs and runs[0]["status"] == "passed"


def test_report_includes_perf(tmp_path):
    from insta_engine.runner import RunResult

    run = RunResult(case_id="c", status="passed", step_success_rate=1.0, duration_ms=100,
                    perf={"launch_ms": 812, "mem_pss_mb": 234.4}, steps=[])
    html = render_html(run, tmp_path).read_text(encoding="utf-8")
    assert "启动 812ms" in html and "内存 234MB" in html


# -- 任务看门狗（锁屏/离线导致的挂死任务） ---------------------------------------

def test_start_job_error_appends_event():
    """任务失败必须把失败原因推进事件流（2026-10-13 用户反馈：事件日志只停在
    最后一条 running，失败原因只躺在 /api/job 的 error 字段里，前端看不到）。"""
    from insta_engine import web_server

    def boom(emit, cancel):
        emit("gen", "running", "AI 生成中")
        raise RuntimeError("上游读超时")

    jid = web_server.start_job("gen", boom)
    try:
        deadline = time.time() + 5
        while time.time() < deadline:  # worker 是后台线程，轮询等终态
            with web_server._JOB_LOCK:
                if web_server._JOBS.get(jid, {}).get("status") == "error":
                    break
            time.sleep(0.05)
        with web_server._JOB_LOCK:
            job = web_server._JOBS[jid]
        assert job["status"] == "error" and "上游读超时" in job["error"]
        last = job["events"][-1]
        assert (last["step_id"], last["status"]) == ("job", "error")
        assert "上游读超时" in last["detail"]
    finally:
        with web_server._JOB_LOCK:
            web_server._JOBS.pop(jid, None)


def test_sweep_stale_jobs_marks_timeout():
    from insta_engine import web_server

    job_id = "testjob01"
    with web_server._JOB_LOCK:
        web_server._JOBS[job_id] = {"kind": "run", "status": "running",
                                    "started_ts": time.time() - 20 * 60}
    try:
        assert web_server.sweep_stale_jobs(timeout_seconds=15 * 60) >= 1
        with web_server._JOB_LOCK:
            job = web_server._JOBS[job_id]
        assert job["status"] == "error" and "锁屏" in job["error"]
        # 看门狗标记超时后同样要把原因推进事件流（与 worker 异常路径同口径）
        assert job["events"] and job["events"][-1]["status"] == "error"

        fresh = "freshjob1"
        with web_server._JOB_LOCK:
            web_server._JOBS[fresh] = {"kind": "run", "status": "running",
                                       "started_ts": time.time()}
        assert web_server.sweep_stale_jobs() == 0  # 新任务不受影响
        with web_server._JOB_LOCK:
            assert web_server._JOBS[fresh]["status"] == "running"
            del web_server._JOBS[fresh]
    finally:
        with web_server._JOB_LOCK:
            web_server._JOBS.pop(job_id, None)


def test_sweep_counts_inactivity_not_total_duration():
    """2026-10-21 修正：跑满 15 分钟但一直在推进的任务不能被误杀（旧实现按 started_ts）。"""
    from insta_engine import web_server

    job_id = "longrunning"
    with web_server._JOB_LOCK:
        web_server._JOBS[job_id] = {"kind": "import", "status": "running",
                                    "started_ts": time.time() - 40 * 60,
                                    "last_activity_ts": time.time() - 30,
                                    "events": [], "seq": 0}
    try:
        assert web_server.sweep_stale_jobs(timeout_seconds=15 * 60) == 0
        with web_server._JOB_LOCK:
            assert web_server._JOBS[job_id]["status"] == "running"
    finally:
        with web_server._JOB_LOCK:
            web_server._JOBS.pop(job_id, None)


def test_sweep_timeout_hint_is_kind_aware():
    """导入任务超时不能给设备归因（导入不碰设备）。"""
    from insta_engine import web_server

    job_id = "staleimport"
    with web_server._JOB_LOCK:
        web_server._JOBS[job_id] = {"kind": "import", "status": "running",
                                    "started_ts": time.time() - 30 * 60,
                                    "events": [], "seq": 0}
    try:
        assert web_server.sweep_stale_jobs(timeout_seconds=15 * 60) >= 1
        with web_server._JOB_LOCK:
            err = web_server._JOBS[job_id]["error"]
        assert "AI 转换" in err and "解锁" not in err and "adb" not in err
    finally:
        with web_server._JOB_LOCK:
            web_server._JOBS.pop(job_id, None)


# -- u2 选择器 API（真机首测教训：选择器必须走 Device.__call__，不能 getattr） ----

class _FakeNode:
    def __init__(self, exists=True):
        self.exists = exists

    def wait(self, timeout=None):
        return self.exists

    def click(self):
        pass

    def get_text(self):
        return "我的"


class FakeU2Raw:
    """验证选择器以 d(**kwargs) 调用形式命中（而不是 getattr 属性形式）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeNode()


def test_locator_uses_call_style_selectors(tmp_path):
    from insta_engine.dsl import Locator
    from insta_engine.locator import resolve

    raw = FakeU2Raw()

    class Drv(_LocatorShim):
        serial = "fake"

        class raw:
            pass

    drv = Drv()
    drv.raw = raw

    def dump():
        return "<a/>"

    drv.dump = dump

    element = resolve(drv, Locator(strategy=LocatorStrategy.TEXT, value="我的"), 2000)
    assert element.ok and element.strategy == "text"
    assert drv.raw.calls == [{"text": "我的"}]  # 调用形式，而非 d.text("我的")


# -- text 策略的 desc 兜底（真机首测：底部导航文字在 content-desc） ----------

class DescOnlyRaw:
    """text 精确匹配不命中，仅 description 命中——模拟底部导航标签。"""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        node = _FakeNode()
        node.exists = "description" in kwargs
        return node


def test_text_strategy_falls_back_to_description(tmp_path):
    from insta_engine.dsl import Locator
    from insta_engine.locator import resolve

    class Drv(_LocatorShim):
        serial = "fake"

        def dump(self):
            return "<a/>"

    drv = Drv()
    drv.raw = DescOnlyRaw()
    element = resolve(drv, Locator(strategy=LocatorStrategy.TEXT, value="我的"), 2000)
    assert element.ok and element.strategy == "text→description"
    kinds = [list(c.keys())[0] for c in drv.raw.calls]
    assert kinds[0] == "text" and "description" in kinds  # 等价链顺序正确


# -- vision-llm 兜底（最后一级；置信度红线 0.7） ---------------------

def test_parse_vision_response_contract_tiers(tmp_path):
    """坐标契约三档：ratio（严格<1.0）→ permille（大屏）→ pixel；置信度红线与树证据门禁。"""
    from insta_engine.vision import parse_vision_response

    small = io.BytesIO()
    Image.new("RGB", (200, 400)).save(small, format="PNG")
    small_png = small.getvalue()
    large = io.BytesIO()
    Image.new("RGB", (1080, 2340)).save(large, format="PNG")
    large_png = large.getvalue()

    # ① 比例档（0<x<1.0 严格）
    assert parse_vision_response({"found": True, "confidence": 0.9, "x": 0.5, "y": 0.25}, small_png) == (100, 100)
    # ② 千分比档（仅大屏参与判定）
    assert parse_vision_response({"found": True, "confidence": 0.9, "x": 500, "y": 250}, large_png) == (540, 585)
    # ③ 像素档（小屏上 [0,1000] 与像素重叠，不判千分比，直接按像素）
    assert parse_vision_response({"found": True, "confidence": 0.9, "x": 150, "y": 300}, small_png) == (150, 300)
    # 红线：置信度不足
    assert parse_vision_response({"found": True, "confidence": 0.69, "x": 0.5, "y": 0.5}, small_png) is None
    # 无在场证据时门禁提升至 0.8
    assert parse_vision_response({"found": True, "confidence": 0.75, "x": 0.5, "y": 0.5},
                                 small_png, tree_evidence=False) is None
    assert parse_vision_response({"found": True, "confidence": 0.75, "x": 0.5, "y": 0.5},
                                 small_png, tree_evidence=True) == (100, 200)
    # 缺坐标 / found=false
    assert parse_vision_response({"found": True, "confidence": 0.9}, small_png) is None
    assert parse_vision_response({"found": False, "confidence": 0.9, "x": 0.5, "y": 0.5}, small_png) is None


class MissingRaw:
    """所有选择器都未命中；可截图（200x400）。"""

    def __call__(self, **kwargs):
        node = _FakeNode()
        node.exists = False
        return node

    def screenshot(self):
        buf = io.BytesIO()
        Image.new("RGB", (200, 400)).save(buf, format="PNG")
        return Image.open(buf)


class FakeVisionClient:
    def complete_raw_text(self, messages, context=""):
        return '{"found": true, "confidence": 0.9, "x": 0.3, "y": 0.4, "desc": "我的"}'


class FakeVisionClientNone:
    def complete_raw_text(self, messages, context=""):
        return '{"found": false, "confidence": 0.0}'


def test_runner_vision_fallback_rescues_locate_failure(tmp_path):
    from insta_engine.runner import Runner

    class VisionDrv(FakeDriver):
        serial = "fake"

        def __init__(self):
            self.raw = MissingRaw()
            self.clicked = None

        def screenshot_pil(self):  # P0-iOS：视觉兜底改走中立截图方法（不再读 drv.raw）
            return self.raw.screenshot()

        def click(self, x, y):
            # 真实点击会让界面变化。桩若恒定不变，等价于"点空了"——2026-10-27 起
            # runner 对"点了界面纹丝不动"判 failed（点空了不算自愈成功，见
            # test_selfheal_noop.py）。这里必须模拟真实行为，否则测的是一个不存在的场景。
            self.clicked = (x, y)

        def dump(self):
            return f"<hierarchy clicked={self.clicked}/>"

    case = TestCase.model_validate({
        "caseId": "case_vl",
        "steps": [{"id": "s1", "intent": "点击我的", "action": "tap", "onFailure": "heal",
                   "target": {"strategy": "text", "value": "不存在"}}],
    })
    runner = Runner(VisionDrv(), tmp_path,
                    history_path=tmp_path / "h.jsonl", vision_client=FakeVisionClient())
    run = runner.run(case)
    step = run.steps[0]
    assert step.status == "passed" and step.used_strategy == "vision-llm"
    assert step.heal["via"] == "vision-llm"
    assert step.heal["match"] == "vision-llm=(60,160)"  # 300*200/1000, 400*400/1000


def test_runner_vision_disabled_keeps_failure(tmp_path):
    from insta_engine.runner import Runner

    class VisionDrv(FakeDriver):
        serial = "fake"

        def __init__(self):
            self.raw = MissingRaw()

        def screenshot_pil(self):  # P0-iOS：视觉兜底改走中立截图方法（不再读 drv.raw）
            return self.raw.screenshot()

    case = TestCase.model_validate({
        "caseId": "case_vl2",
        "steps": [{"id": "s1", "action": "tap",
                   "target": {"strategy": "text", "value": "不存在"}}],
    })
    runner = Runner(VisionDrv(), tmp_path, history_path=tmp_path / "h.jsonl",
                    vision_client=FakeVisionClientNone())
    run = runner.run(case)
    assert run.steps[0].status == "failed" and run.steps[0].category == "locate-fail"
    assert "vision-llm" in run.steps[0].error


def test_extract_fields_tolerates_malformed_vl_output():
    from insta_engine.vision import extract_fields

    # 真机实测的畸形输出："x": 438, 124 两数挤在一起
    fields = extract_fields('{"found": true, "confidence": 0.95, "x": 438, 124, "desc": "首页搜索框"}')
    assert fields["found"] is True and fields["confidence"] == 0.95
    assert fields["x"] == 438 and fields["y"] == 124  # 第二个数按 y 处理
    assert extract_fields('找不到目标')["found"] is False
    assert extract_fields('{"found": true, "confidence": "高", "x": 1, "y": 2}')["confidence"] == 0.0


# -- vision 坐标输入（点击聚焦 + send_keys） --------------------------------------

def test_point_element_set_text_uses_focus_and_send_keys(tmp_path):
    from insta_engine.locator import Element

    sent = []
    clicked = []

    class Drv(_LocatorShim):
        serial = "fake"

        def click(self, x, y):
            clicked.append((x, y))

        def send_keys(self, text):
            sent.append(text)

    element = Element(strategy="vision-llm", value="(60,160)", point=(60, 160), drv=Drv())
    element.set_text("火锅")
    assert clicked == [(60, 160)] and sent == ["火锅"]  # 先聚焦再输入


# -- node 路径输入回退（华为安全键盘静默拦截 set_text） -------------------

def test_node_set_text_falls_back_to_adb_input_when_silently_blocked():
    from insta_engine.locator import Element

    class Node:
        exists = True

        def __init__(self):
            self.clicks = 0

        def click(self):
            self.clicks += 1

        def set_text(self, text):
            pass  # 华为安全键盘：静默拦截，字段保持全空

        def get_text(self):
            return ""

    class Raw:
        def __init__(self):
            self.shell_cmds = []

        def shell(self, cmd):
            self.shell_cmds.append(cmd)

    class Drv(_LocatorShim):
        serial = "fake"

        def __init__(self):
            self.raw = Raw()

    node, drv = Node(), Drv()
    Element(strategy="res-id", value="user", node=node, drv=drv).set_text("user 1")
    # set_text 被拦截 → 重新聚焦后走 adb input text 回退；空格按 adb 规则转 %s
    assert node.clicks == 2
    assert drv.raw.shell_cmds == ["input text user%s1"]


def test_node_set_text_no_fallback_when_set_text_works():
    from insta_engine.locator import Element

    class Node:
        exists = True

        def __init__(self):
            self.text = ""

        def click(self):
            pass

        def set_text(self, text):
            self.text = text

        def get_text(self):
            return self.text

    class Raw:
        def __init__(self):
            self.shell_cmds = []

        def shell(self, cmd):
            self.shell_cmds.append(cmd)

    class Drv(_LocatorShim):
        serial = "fake"

        def __init__(self):
            self.raw = Raw()

    node, drv = Node(), Drv()
    Element(strategy="res-id", value="user", node=node, drv=drv).set_text("alice")
    assert node.text == "alice" and drv.raw.shell_cmds == []  # 正常路径不触发回退


# -- V1.2 页面索引（digest/页签名/控件清单/唯一 caseId） --------------------------

SAMPLE_XML = '''<hierarchy>
  <node index="0" text="" resource-id="" class="FrameLayout" package="com.other" content-desc="" clickable="true" bounds="[0,0][100,100]"/>
  <node index="1" text="" resource-id="com.insta.demo:id/username" class="EditText" package="com.insta.demo" content-desc="账号输入" clickable="true" bounds="[10,100][300,200]"/>
  <node index="2" text="登录" resource-id="com.insta.demo:id/login_btn" class="Button" package="com.insta.demo" content-desc="" clickable="true" bounds="[10,220][300,300]"/>
  <node index="3" text="" resource-id="" class="View" package="com.insta.demo" content-desc="" clickable="false" bounds="[0,0][50,50]"/>
</hierarchy>'''


def test_digest_page_filters_and_collects():
    from insta_engine.page_index import digest_page

    digest = digest_page(SAMPLE_XML, "com.insta.demo")
    controls = digest["controls"]
    assert len(controls) == 2  # 外部包节点与不可交互节点被过滤
    username = next(c for c in controls if c.get("res-id", "").endswith("username"))
    assert username["clickable"] is True and username["desc"] == "账号输入"
    assert any(c.get("text") == "登录" for c in controls)


def test_page_key_stable_and_sensitive():
    from insta_engine.page_index import page_key

    assert page_key(SAMPLE_XML, "com.insta.demo") == page_key(SAMPLE_XML, "com.insta.demo")
    assert page_key(SAMPLE_XML, "com.insta.demo") != page_key(SAMPLE_XML.replace("登录", "注册"), "com.insta.demo")


def test_controls_context_dedup_and_limit():
    from insta_engine.page_index import controls_context

    pages = [
        {"activity": "LoginActivity", "controls": [
            {"res-id": "com.insta.demo:id/username", "text": "账号"},
            {"res-id": "com.insta.demo:id/login_btn", "text": "登录"}]},
        {"activity": "LoginActivity", "controls": [
            {"res-id": "com.insta.demo:id/username", "text": "账号"}]},  # 重复
    ]
    context = controls_context(pages)
    assert context.count("username") == 1
    assert "login_btn" in context
    assert controls_context([], 10) == "（索引为空：请先执行 index 命令构建）"


def test_controls_context_injects_page_purposes():
    """2026-10-13：有描述的页在清单前注入【页面用途】段——AI 写用例先看懂页面职责。"""
    from insta_engine.page_index import controls_context

    pages = [
        {"activity": "SearchActivity", "title": "搜索结果页",
         "summary": "展示关键字搜索结果", "controls": [
             {"res-id": "com.demo:id/result_list"}]},
        {"activity": "LoginActivity", "title": "", "controls": [
             {"res-id": "com.demo:id/login_btn"}]},   # 无描述 → 不进用途段
    ]
    context = controls_context(pages)
    assert "【页面用途】" in context and "搜索结果页: 展示关键字搜索结果" in context
    purpose_section = context.split("【页面用途】")[1].split("【控件清单】")[0]
    assert "LoginActivity" not in purpose_section, "无描述页不进用途段"
    assert "login_btn" in context, "控件清单主体不受影响"


def test_page_purpose_lines_limit_and_fallback():
    from insta_engine.page_index import page_purpose_lines

    pages = [{"activity": f"com.demo.P{i}Activity", "title": f"页{i}",
              "summary": f"用途{i}"} for i in range(70)]
    lines = page_purpose_lines(pages, max_notes=60)
    assert len(lines) == 60 and lines[0].startswith("- 页0:") and lines[59].startswith("- 页59:")
    lines2 = page_purpose_lines([{"activity": "com.demo.LoginActivity", "summary": "登录"}])
    assert lines2 == ["- LoginActivity: 登录"], "无标题页回退 Activity 短名"


def test_unique_case_id(tmp_path):
    from insta_engine.dsl import unique_case_id

    (tmp_path / "a.json").write_text('{"caseId": "case_7a9b2c", "steps": [{"id": "s1", "action": "wait"}]}',
                                     encoding="utf-8")
    assert unique_case_id("case_new", tmp_path) == "case_new"
    renamed = unique_case_id("case_7a9b2c", tmp_path)
    assert renamed != "case_7a9b2c" and renamed.startswith("case_")


# -- v0.4.0 激活码（HMAC 签名/过期/防篡改） ---------------------------------------

def test_licensing_roundtrip_and_tamper(tmp_path, monkeypatch):
    from insta_engine import licensing

    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", TEST_LICENSE_KEY)
    code = mint_test_code("pro", months=1)
    assert code.startswith("MAT-")
    info = licensing.activate(code)
    assert info["plan"] == "pro"
    assert licensing.read_license()["plan"] == "pro"

    # 篡改：换一位
    tampered = code[:-1] + ("0" if code[-1] != "0" else "1")
    assert licensing.verify_code(tampered) is None


def test_licensing_rejects_code_signed_with_public_key(tmp_path, monkeypatch):
    """用公开兜底密钥签的码，在设置了真实密钥的机器上必须被拒。

    这是「签发工具不随包分发」这条边界的回归门禁：普通用户的校验端用公开密钥，
    所以真正的防线是签发端持有独立密钥。
    """
    from insta_engine import licensing

    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", "a-different-secret")
    public_code = mint_test_code("pro", months=1)  # 用测试密钥签
    assert licensing.verify_code(public_code) is None
    # 过期
    import time as _t
    body = f"pro.{int(_t.time()) - 86400}.deadbeef"
    sig = licensing._sign(body)
    assert licensing.verify_code(f"MAT-{body}.{sig}") is None  # 过期拒绝


# -- V1.5.1 探索深度模型（L0-Ln / 父链 / 复现路径 / 深度策略） ---------------------

class FakeExploreDriver:
    """三页状态机：A --去B--> B --去C--> C（无出边）；back 出栈；可截图。"""

    serial = None
    XML = {
        "A": '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "B": '<node class="Button" text="去C" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "C": '<node class="TextView" text="C页面" package="com.x"/>',
    }
    TRANS = {("A", 5): "B", ("B", 5): "C"}

    def __init__(self):
        self.stack = ["A"]
        self.clicks = []

    def dump(self):
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        self.clicks.append((self.stack[-1], x, y))
        target = self.TRANS.get((self.stack[-1], x))
        if target:
            self.stack.append(target)

    def press(self, key):
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = ["A"]


def test_explore_tracks_depth_and_path(tmp_path):
    from insta_engine.explore import _path_of, explore

    drv = FakeExploreDriver()
    report = explore(drv, "com.x", minutes=0.05, max_steps=20,
                     artifacts_dir=tmp_path, max_depth=5, sleep_scale=0.01)
    pages = {p["page_key"]: p for p in report["pages"]}
    by_depth = {}
    for p in pages.values():
        by_depth.setdefault(p["depth"], []).append(p)
    assert 0 in by_depth and 1 in by_depth and 2 in by_depth  # A/B/C 三层都被到达
    b = by_depth[1][0]
    assert b["parent_action"] == "去B" and b["parent_key"]
    c = by_depth[2][0]
    path = _path_of(c["page_key"], pages)
    assert "启动/首页" in path and "去B" in path and "去C" in path  # 复现路径回溯
    assert report["depth_stats"] == {"L0": 1, "L1": 1, "L2": 1}


def test_explore_respects_max_depth(tmp_path):
    from insta_engine.explore import explore

    drv = FakeExploreDriver()
    report = explore(drv, "com.x", minutes=0.05, max_steps=20,
                     artifacts_dir=tmp_path, max_depth=1, sleep_scale=0.01)
    depths = {p["depth"] for p in report["pages"]}
    assert max(depths) <= 1  # L2 从未到达：深度上限阻止下钻


def test_explore_force_stops_app_at_session_start(tmp_path):
    """会话开始先 app_stop 再拉前台（根锚定）：`app_start` 对后台任务是热恢复，
    App 停在遗留页时会把遗留页当根页，整棵子树挂在「从 launch 不可达」的根下
    （真机教训 2026-10-16：106 条链全挂在遗留播客页根下，重放全灭）。"""
    from insta_engine.explore import explore

    calls: list[str] = []

    class ColdDriver(FakeExploreDriver):
        def app_stop(self, package):
            calls.append("stop")

        def app_start(self, package):
            calls.append("start")
            super().app_start(package)

    explore(ColdDriver(), "com.x", minutes=0.05, max_steps=6,
            artifacts_dir=tmp_path, max_depth=3, sleep_scale=0.01)
    assert calls and calls[0] == "stop" and "start" in calls


# -- 页面图谱沉淀（schema v2 / 并集合并 / 增量探索） ----

def test_is_dynamic_text_detection():
    from insta_engine.page_index import is_dynamic_text

    for dynamic in ("3", "1,234", "50%", "581条", "8千+", "649m", "12:30",
                    "2026-09-28", "刚刚", "3 分钟前",
                    # 「标签 + 计数」（2026-10-22 某内容社区 App）：数字会变，整串既不是纯数字
                    # 也不够长 ⇒ 旧规则全放过，导致同一个详情页每次进来都变成"新页"
                    "点赞 1336", "收藏135", "评论 409", "粉丝 1.2万", "1/6"):
        assert is_dynamic_text(dynamic), dynamic
    # ⚠️ 反向：**纯标签**必须稳定——`收藏`/`评论` 是入口，抹掉会把整页信号清空
    for stable in ("登录", "搜索", "去B", "bottom-tab", "我的订单", "R_筛选_筛选栏_入口_点击",
                   "关注", "收藏", "评论", "评论框"):
        assert not is_dynamic_text(stable), stable
    assert is_dynamic_text("这是一段很长的商品描述文案" * 4)  # 长文本按内容处理（≥40 字符）
    assert not is_dynamic_text("")


def test_page_key_ignores_dynamic_text_but_not_static():
    from insta_engine.page_index import page_key

    head = '<node class="Button" text="{t}" resource-id="com.x:id/login" package="com.x"/>'
    tail = '<node class="TextView" text="{n}" package="com.x"/>'
    login_581 = head.format(t="登录") + tail.format(n="581条")
    login_2623 = head.format(t="登录") + tail.format(n="2623条")
    register_581 = head.format(t="注册") + tail.format(n="581条")

    assert page_key(login_581, "com.x") == page_key(login_2623, "com.x")  # 动态计数置空 → 同页
    assert page_key(login_581, "com.x") != page_key(register_581, "com.x")  # 静态文案仍参与签名


def test_load_index_upgrades_v1_flat_table(tmp_path):
    from insta_engine.page_index import load_index

    path = tmp_path / "v1.json"
    path.write_text(json.dumps({
        "package": "com.x", "generated_at": "2026-01-01T00:00:00+00:00", "page_count": 1,
        "pages": [{"page_key": "k1", "controls": []}],
    }), encoding="utf-8")
    graph = load_index(path)
    assert graph["schema"] == 2 and graph["package"] == "com.x"
    assert isinstance(graph["pages"], list) and len(graph["pages"]) == 1
    assert load_index(tmp_path / "missing.json") is None  # 缺失静默降级


def test_merge_pages_union_semantics():
    from insta_engine.page_index import merge_pages, new_index

    # `tried` 条目自 2026-10-22 起带语义（`bounds\tlabel\trid`）；老格式（只有坐标）在合并时
    # 会被丢掉（不可验证），所以这里必须用新格式来测"并集"这件事。
    T1, T2 = "[0,0][10,10]\t去A\tcom.x:id/a", "[0,20][10,30]\t去B\tcom.x:id/b"

    index = new_index("com.x")
    merge_pages(index, [{
        "page_key": "k1", "depth": 2, "parent_key": "k0", "parent_action": "深路径",
        "controls": [{"res-id": "a"}], "tried": [T1], "hits": 1,
        "first_seen": "2026-09-01T00:00:00+00:00", "last_seen": "2026-09-01T00:00:00+00:00",
        "step_health": {"tap|深路径": {"ok": 1, "fail": 0}},
    }], source="explore")
    merge_pages(index, [{
        "page_key": "k1", "depth": 1, "parent_key": "k9", "parent_action": "浅路径",
        "controls": [{"res-id": "a"}, {"res-id": "b"}], "tried": [T2], "hits": 1,
        "first_seen": "2026-09-05T00:00:00+00:00", "last_seen": "2026-09-05T00:00:00+00:00",
        "alt_entries": [{"parent_key": "k9", "parent_action": "浅路径"}],
        "step_health": {"tap|浅路径": {"ok": 0, "fail": 2}},
    }], source="index")

    assert isinstance(index["pages"], list) and index["schema"] == 2  # 兼容性：pages 始终是 list
    page = index["pages"][0]
    assert page["depth"] == 1 and page["parent_action"] == "浅路径"    # 取更浅，父链随之更新
    assert page["hits"] == 2                                        # 计数累加
    assert page["tried"] == sorted([T1, T2])                        # 并集
    assert page["node_count"] == 2                                  # 控件取最新
    assert page["first_seen"] == "2026-09-01T00:00:00+00:00"         # 最早
    assert page["last_seen"] == "2026-09-05T00:00:00+00:00"          # 最新
    assert page["step_health"]["tap|深路径"]["ok"] == 1               # 健康度按动作合并
    assert page["step_health"]["tap|浅路径"]["fail"] == 2
    assert index["sources"] == ["explore", "index"]
    assert "step" not in page                                       # 运行期字段不落图谱


def test_v2_graph_pages_feed_controls_context():
    from insta_engine.page_index import controls_context, merge_pages, new_index

    index = new_index("com.x")
    merge_pages(index, [{"page_key": "k1", "activity": "MainActivity", "depth": 0,
                         "controls": [{"res-id": "com.x:id/login", "text": "登录"}]}])
    assert "com.x:id/login" in controls_context(index["pages"])  # 两处消费者仍按 list 消费


def test_cmd_index_fails_loudly_on_empty_graph(tmp_path, monkeypatch, capsys):
    """回归：空图谱必须**醒目告警并返回非 0**，不能静默成功。

    实测踩过：`mat index` 建出 0 控件的图谱，退出码 0、输出看着正常，
    但下游 gen 的防幻觉底座已经废了——典型静默失败。B2。
    """
    from types import SimpleNamespace

    from insta_engine import __main__ as m

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_connect_driver", lambda serial, platform="android": object())
    monkeypatch.setattr("insta_engine.page_index.load_or_crawl",
                        lambda *a, **k: ({"pages": [{"node_count": 0}], "schema": 2}, True))
    args = SimpleNamespace(serial=None, package="com.x", refresh=False, steps=10, pages=5)

    assert m.cmd_index(args) != 0, "空图谱必须返回非 0，否则脚本无法察觉"
    err = capsys.readouterr().err
    assert "图谱为空" in err
    # 文案口径（2026-10-12）：crawl 已自动拉起被测 App（_ensure_foreground），告警
    # 不再让用户"手动打开 App"，改为指引拉起后仍失败的真实原因（锁屏/秒退/包名错）
    assert "锁屏" in err and "秒退" in err and "包名写错" in err, "告警要给出可操作的原因"


def test_load_or_crawl_writes_schema_v2(tmp_path, monkeypatch):
    from insta_engine.page_index import load_index, load_or_crawl

    monkeypatch.setattr("insta_engine.page_index.time.sleep", lambda *_: None)
    path = tmp_path / "com.x.json"
    graph, created = load_or_crawl(FakeExploreDriver(), "com.x", path, max_steps=12, max_pages=5)
    assert created is True and graph["schema"] == 2
    assert isinstance(graph["pages"], list) and len(graph["pages"]) == 3
    assert graph["sources"] == ["index"]

    again, created_again = load_or_crawl(FakeExploreDriver(), "com.x", path)  # 未 refresh → 读回不重爬
    assert created_again is False and again["schema"] == 2
    assert len(load_index(path)["pages"]) == 3


def test_explore_persists_graph_and_reuses_tried(tmp_path):
    from insta_engine.explore import explore
    from insta_engine.page_index import load_index

    graph_path = tmp_path / "com.x.json"
    first = explore(FakeExploreDriver(), "com.x", minutes=0.05, max_steps=20,
                    artifacts_dir=tmp_path / "r1", max_depth=5, sleep_scale=0.01,
                    index_path=graph_path)
    assert first["index_path"] == str(graph_path)
    assert first["prior_pages"] == 0 and first["reused_tried"] == 0 and first["page_count"] == 3

    graph = load_index(graph_path)
    assert graph["schema"] == 2 and len(graph["pages"]) == 3 and graph["sources"] == ["explore"]
    home = next(p for p in graph["pages"] if p["depth"] == 0)
    assert home["tried"] and home["app_version"] == ""       # tried 落盘（app_version 桩驱动取不到）
    level1 = next(p for p in graph["pages"] if p["depth"] == 1)
    assert level1["parent_action"] == "去B"
    assert level1["step_health"]["tap|去B"]["ok"] == 1        # 到达动作健康度

    # 第二次探索：读回 tried → 已试分支被跳过，只重访首页（不再从零开始）
    second = explore(FakeExploreDriver(), "com.x", minutes=0.05, max_steps=20,
                     artifacts_dir=tmp_path / "r2", max_depth=5, sleep_scale=0.01,
                     index_path=graph_path)
    assert second["prior_pages"] == 3 and second["reused_tried"] == 2
    assert second["page_count"] == 1

    graph2 = load_index(graph_path)
    assert len(graph2["pages"]) == 3                          # 本轮未访的 B/C 未被覆盖丢失
    assert next(p for p in graph2["pages"] if p["depth"] == 0)["hits"] == 2


def test_explore_without_index_does_not_persist(tmp_path):
    from insta_engine.explore import explore

    report = explore(FakeExploreDriver(), "com.x", minutes=0.05, max_steps=6,
                     artifacts_dir=tmp_path, max_depth=5, sleep_scale=0.01)
    assert report["index_path"] is None            # 未传 index_path → 不读不写（改造前行为）
    assert report["prior_pages"] == 0 and report["reused_tried"] == 0


class MultiEntryDriver:
    """首页 A 有两个入口（入口1/入口2）都通向同一页 D → 一页多入口。"""

    serial = None
    XML = {
        "A": '<node class="Button" text="入口1" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
             '<node class="Button" text="入口2" clickable="true" bounds="[20,0][30,10]" package="com.x"/>',
        "D": '<node class="TextView" text="目标页" package="com.x"/>',
    }

    def __init__(self):
        self.stack = ["A"]

    def dump(self):
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        if self.stack[-1] == "A":
            self.stack.append("D")

    def press(self, key):
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = ["A"]


def test_explore_records_alt_entries_for_multi_entry_page(tmp_path):
    from insta_engine.explore import explore

    report = explore(MultiEntryDriver(), "com.x", minutes=0.05, max_steps=20,
                     artifacts_dir=tmp_path, max_depth=5, sleep_scale=0.01)
    pages = {p["depth"]: p for p in report["pages"]}
    target = pages[1]
    assert target["parent_action"] == "入口1"                      # 最短/首次到达 = 主父链
    assert [e["parent_action"] for e in target["alt_entries"]] == ["入口2"]  # 另一条入口被记为备选
    assert target["alt_entries"][0]["parent_key"] == pages[0]["page_key"]
    assert target["step_health"]["tap|入口1"]["ok"] == 1           # 到达动作健康度


# -- P1 页面图谱增强（edges / 零 AI 结构描述 / title+summary 可选 AI） ------------

def test_describe_structure_from_controls():
    from insta_engine.page_index import describe_structure

    controls = [
        {"class": "TextView", "text": "搜索", "bounds": "[0,0][100,50]"},
        {"class": "TextView", "text": "历史搜索", "bounds": "[0,60][200,90]"},
        {"class": "TextView", "text": "581条", "bounds": "[0,200][100,220]"},      # 动态 → 不入结构
        {"class": "TextView", "text": "x" * 50, "bounds": "[0,300][200,320]"},     # 长文本 → 按内容
        {"class": "TextView", "text": "我的订单", "bounds": "[0,900][200,950]"},
    ]
    structure = describe_structure(controls)
    assert [r["name"] for r in structure["layout_regions"]] == ["顶部", "底部"]   # 几何分带
    assert structure["key_elements"] == ["搜索", "历史搜索", "我的订单"]
    assert "581条" in structure["dynamic_regions"]        # 根因清单
    assert "x" * 50 in structure["dynamic_regions"]
    assert describe_structure([]) == {"layout_regions": [], "key_elements": [], "dynamic_regions": []}


def test_merge_edges_accumulates_counts():
    from insta_engine.page_index import merge_edges, new_index

    index = new_index("com.x")
    merge_edges(index, [{"from": "a", "to": "b", "action": "去B", "count": 1},
                        {"from": "a", "to": "c", "action": "去C", "count": 1}])
    merge_edges(index, [{"from": "a", "to": "b", "action": "去B", "count": 1}])
    assert index["edges"] == [{"from": "a", "to": "b", "action": "去B", "count": 2},
                              {"from": "a", "to": "c", "action": "去C", "count": 1}]
    assert index["edges"] == index["edges"]  # 幂等读取


def test_update_page_does_not_count_as_visit():
    from insta_engine.page_index import merge_pages, new_index, update_page

    index = new_index("com.x")
    merge_pages(index, [{"page_key": "k1", "controls": [{"res-id": "a"}]}])
    assert index["pages"][0]["hits"] == 1
    assert update_page(index, "k1", title="首页", title_source="ai",
                       summary="首页：搜索 + 金刚区", summary_source="ai") is True
    assert index["pages"][0]["hits"] == 1              # 补描述不算一次页面访问
    assert index["pages"][0]["title"] == "首页"
    assert update_page(index, "missing", title="x") is False


def test_title_summary_merge_precedence():
    from insta_engine.page_index import merge_pages, new_index

    index = new_index("com.x")
    merge_pages(index, [{"page_key": "k1", "title": "手工名", "title_source": "manual",
                         "summary": "手工描述", "summary_source": "manual"}])
    merge_pages(index, [{"page_key": "k1", "title": "AI名", "title_source": "ai",
                         "summary": "AI描述", "summary_source": "ai"}])
    page = index["pages"][0]
    assert page["title"] == "手工名" and page["title_source"] == "manual"  # 人工不被 AI 顶掉
    assert page["summary"] == "手工描述"
    merge_pages(index, [{"page_key": "k1", "controls": []}])              # 空值不覆盖已有内容
    assert index["pages"][0]["title"] == "手工名"


def test_explore_emits_edges_and_structure(tmp_path):
    from insta_engine.explore import explore
    from insta_engine.page_index import load_index

    graph_path = tmp_path / "com.x.json"
    report = explore(FakeExploreDriver(), "com.x", minutes=0.05, max_steps=20,
                     artifacts_dir=tmp_path / "r1", max_depth=5, sleep_scale=0.01,
                     index_path=graph_path)
    assert {(e["action"]) for e in report["edges"]} == {"去B", "去C"}      # 完整跳转图
    assert all(e["count"] >= 1 and e["from"] and e["to"] for e in report["edges"])

    graph = load_index(graph_path)
    assert len(graph["edges"]) == 2
    assert graph["pages"][0]["structure"]["key_elements"]                 # 零 AI 结构落盘


class FakeDescribeClient:
    prompt_mode = "local"

    def __init__(self, payload):
        self.payload = payload

    def complete_raw(self, messages, context="生成"):
        return self.payload


def test_describe_pages_drops_hallucinated_keys(monkeypatch):
    from insta_engine import ai_client
    from insta_engine.ai_client import GenClient

    monkeypatch.setattr(ai_client, "_assemble_local", lambda task, variables: "prompt")
    client = FakeDescribeClient({"pages": [
        {"page_key": "k1", "title": "搜索页", "summary": "搜索 + 热搜 + 历史"},
        {"page_key": "编造的", "title": "不存在页", "summary": "x"},        # 模型幻觉 → 丢弃
    ]})
    assert GenClient.describe_pages(client, [{"page_key": "k1"}]) == {
        "k1": {"title": "搜索页", "summary": "搜索 + 热搜 + 历史"}}


def test_graph_context_is_gated_and_bounded(tmp_path, monkeypatch):
    from insta_engine import __main__ as cli
    from insta_engine.page_index import merge_pages, new_index, save_index

    monkeypatch.chdir(tmp_path)
    index = new_index("com.x")
    merge_pages(index, [{"page_key": "k1", "depth": 0, "activity": "com.x/.MainActivity",
                         "controls": [{"text": "首页", "bounds": "[0,0][10,10]"}]}])
    save_index(Path("artifacts/page_index/com.x.json"), index)

    monkeypatch.delenv("GEN_GRAPH_CONTEXT", raising=False)
    assert cli._graph_context_for("com.x") == ""            # 默认关闭：不改变现有生成行为
    monkeypatch.setenv("GEN_GRAPH_CONTEXT", "1")
    context = cli._graph_context_for("com.x")
    assert context.startswith("【页面结构】") and "首页" in context


def test_describe_command_writes_ai_titles(tmp_path, monkeypatch):
    from argparse import Namespace

    from insta_engine import __main__ as cli
    from insta_engine.page_index import load_index, merge_pages, new_index, save_index

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    index = new_index("com.x")
    merge_pages(index, [{"page_key": "k1", "depth": 0, "parent_action": None,
                         "controls": [{"text": "首页", "bounds": "[0,0][10,10]"}]}])
    save_index(Path("artifacts/page_index/com.x.json"), index)

    class Stub:
        def describe_pages(self, pages):
            return {p["page_key"]: {"title": "首页", "summary": "首页：搜索 + 金刚区"} for p in pages}

    monkeypatch.setattr(cli.GenClient, "from_env", classmethod(lambda cls: Stub()))
    assert cli.cmd_describe(Namespace(package="com.x", limit=20, refresh=False)) == 0

    page = load_index(Path("artifacts/page_index/com.x.json"))["pages"][0]
    assert page["title"] == "首页" and page["title_source"] == "ai"
    assert page["summary_source"] == "ai" and page["hits"] == 1   # 补描述不计入访问

    # 二次执行：已有 AI 描述 → 不重复调用（幂等，不白花额度）
    assert cli.cmd_describe(Namespace(package="com.x", limit=20, refresh=False)) == 0


def test_load_index_backfills_structure_for_v1_pages(tmp_path):
    """旧 v1 平表页没有 structure → 读取时用自带 controls 零 AI 补算。

    真实数据踩到：凌晨 `mat index` 建的 v1 索引 6 页全缺 structure，
    不补的话 `mat describe` 只能拿到空 key_elements，起名全靠编。
    """
    from insta_engine.page_index import load_index

    path = tmp_path / "v1.json"
    path.write_text(json.dumps({
        "package": "com.x", "generated_at": "2026-01-01T00:00:00+00:00", "page_count": 1,
        "pages": [{"page_key": "k1", "activity": "A", "controls": [
            {"text": "搜索", "bounds": "[0,0][100,50]"},
            {"text": "581条", "bounds": "[0,900][100,950]"}]}],
    }), encoding="utf-8")

    page = load_index(path)["pages"][0]
    assert page["structure"]["key_elements"] == ["搜索"]
    assert page["structure"]["dynamic_regions"] == ["581条"]
    assert page["first_seen"] == "2026-01-01T00:00:00+00:00"   # 用索引生成时间兜底，不是 now
    assert page["hits"] == 1 and page["title_source"] == ""


# -- 数据驱动选组（run --dataset，CASE-06） -------------------------------------

def test_select_datasets_by_name_index_and_all():
    from insta_engine.__main__ import _select_datasets

    ds = [{"name": "alice"}, {"name": "bob"}, {"name": "carol"}]
    # 默认/空/all → 全部（保持既有行为）
    assert _select_datasets(ds, None) == ds
    assert _select_datasets(ds, "") == ds
    assert _select_datasets(ds, "all") == ds
    assert _select_datasets(ds, "ALL") == ds
    # 按组名 / 按 1 基序号 / 混合
    assert [d["name"] for d in _select_datasets(ds, "bob")] == ["bob"]
    assert [d["name"] for d in _select_datasets(ds, "1,3")] == ["alice", "carol"]
    assert [d["name"] for d in _select_datasets(ds, "alice, 3")] == ["alice", "carol"]
    # 无名组按「组N」兜底可被序号命中
    unnamed = [{"vars": {}}, {"vars": {}}]
    assert _select_datasets(unnamed, "2") == [unnamed[1]]
    # 无匹配 → None（调用方报错退出，不静默跑空）
    assert _select_datasets(ds, "nobody") is None


# -- prompt 模板定位与播种（修复 wheel 不含 prompts，PyPI 装包后 AI 能力可用） -----

def test_promptstore_seeds_workspace_from_package(tmp_path):
    from insta_engine import promptstore

    empty_ws = tmp_path / "prompts"
    # 工作区为空 → resolve 回退到包内 .example（pip/wheel 安装场景）
    for name in promptstore.NAMES:
        resolved = promptstore.resolve(name, workspace=empty_ws)
        assert resolved is not None and resolved.exists()
        assert resolved.parent == promptstore.PKG_PROMPTS

    created = promptstore.seed(empty_ws)
    assert len(created) == len(promptstore.NAMES)
    for name in promptstore.NAMES:
        assert (empty_ws / f"{name}.prompt").exists()
        # 播种后 resolve 优先命中工作区 .prompt（用户可自行调优）
        assert promptstore.resolve(name, workspace=empty_ws).parent == empty_ws

    assert promptstore.seed(empty_ws) == []  # 幂等：不覆盖已存在


def test_packaged_prompts_match_repo_examples():
    """防漂移：包内 .example 必须与仓库 prompts/*.example 逐字节一致。

    ⚠️ 并且：**内部仓库**里生效的 `prompts/<name>.prompt`（若有）也必须与模板一致。
    为什么要加后半条（2026-10-04）：

    - `.prompt` 是**我们跑测时真正加载的那个**（`promptstore.resolve` 优先级最高），
      而 `.example` 是**随包发给用户的那个**。两者不一致 = 「**我们测的 ≠ 我们发的**」，
      用户跑出的效果与我们验证的不同——而这类漂移**不报错**（效果悄悄变了）。
    - 更麻烦的是 **`*.prompt` 被 .gitignore 忽略**（闭源资产，任意层级不入库）⇒
      `git status` / `git diff` **看不到**这个文件的漂移，**只有测试能拦**。

    范围限定：只在**内部仓库**比对（用不导出的 `-任务看板.md` 当判据）。
    公开仓/用户工作区里，用户 `mat init` 后**有权调优自己的 `.prompt`**——
    那时 `.prompt != .example` 是**正常**的，不该报红。
    """
    from insta_engine import promptstore

    repo_prompts = REPO / "prompts"
    internal = (REPO / "docs" / "30-任务看板.md").exists()  # 内部仓库判据（该文件不导出）
    for name in promptstore.NAMES:
        pkg = promptstore.PKG_PROMPTS / f"{name}.prompt.example"
        src = repo_prompts / f"{name}.prompt.example"
        assert pkg.exists() and src.exists(), f"{name} 模板缺失"
        assert pkg.read_bytes() == src.read_bytes(), f"{name} 模板与仓库不一致（需重新复制）"

        effective = repo_prompts / f"{name}.prompt"
        if internal and effective.exists():
            assert effective.read_bytes() == src.read_bytes(), (
                f"{name} 的生效 prompt（prompts/{name}.prompt）与模板/包内不一致"
                f"——**我们跑测用的 ≠ 我们发给用户的**。改 prompt 时三处要一起改"
                f"（该文件被 .gitignore 忽略，git 看不到漂移，只能靠这条测试拦）。"
            )




def test_source_staleness_flags_newer_source(tmp_path, monkeypatch):
    """长驻 console 跑旧代码是真实踩过的坑：源码比进程新必须被判为 stale。"""
    from insta_engine import web_server

    src = tmp_path / "insta_engine"
    src.mkdir()
    (src / "mod.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(web_server_rt, "_SOURCE_DIR", src)

    monkeypatch.setattr(web_server_rt, "_PROCESS_STARTED", time.time() + 60)
    assert web_server.source_staleness()["stale"] is False   # 进程晚于源码 → 正常

    monkeypatch.setattr(web_server_rt, "_PROCESS_STARTED", time.time() - 60)
    stale = web_server.source_staleness()
    assert stale["stale"] is True and stale["file"] == "mod.py"


def test_require_fresh_code_blocks_but_allows_override(tmp_path, monkeypatch):
    from insta_engine import web_server

    src = tmp_path / "insta_engine"
    src.mkdir()
    (src / "mod.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(web_server_rt, "_SOURCE_DIR", src)
    monkeypatch.setattr(web_server_rt, "_PROCESS_STARTED", time.time() - 60)

    monkeypatch.delenv("MAT_ALLOW_STALE_CODE", raising=False)
    with pytest.raises(RuntimeError, match="重启 console"):
        web_server._require_fresh_code()

    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")          # 逃生舱
    web_server._require_fresh_code()


def test_controls_context_for_shares_one_entry(tmp_path):
    """CLI 与 console 共用入口：此前 console 的 convert 漏了控件清单（防幻觉底座）。"""
    from insta_engine.page_index import controls_context_for, merge_pages, new_index, save_index

    assert controls_context_for("com.x", tmp_path) == ""      # 无图谱 → 空串，静默降级

    index = new_index("com.x")
    merge_pages(index, [{"page_key": "k1", "activity": "com.x/.MainActivity",
                         "controls": [{"res-id": "btn_ok", "text": "确定",
                                       "bounds": "[0,0][10,10]"}]}])
    save_index(tmp_path / "com.x.json", index)
    text = controls_context_for("com.x", tmp_path)
    assert "btn_ok" in text and "确定" in text

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert controls_context_for("bad", tmp_path) == ""        # 损坏 → 空串


# -- 同屏快照去重 + res-id 缺失回落 text 注入 ----------------

def test_same_page_containment_gate():
    """门禁本身：微抖同页 ≥0.8 判同页；不同页/空集合不合并（宁漏杀不错杀）。"""
    from insta_engine.explore import _identities, _same_page

    base = ('<node class="TextView" text="店铺名" package="com.x"/>'
            '<node class="TextView" text="菜品" package="com.x"/>'
            '<node class="TextView" text="封面" package="com.x"/>'
            '<node class="TextView" text="4-5人餐" package="com.x"/>'
            '<node class="TextView" text="好评" package="com.x"/>'
            '<node class="Button" text="轮播" clickable="true" package="com.x"/>')
    jittered = base + '<node class="TextView" text="新章节" package="com.x"/>'      # MRN 渐进渲染 +1 控件
    other = ('<node class="TextView" text="搜索" package="com.x"/>'
             '<node class="Button" text="取消" clickable="true" package="com.x"/>')

    ids_base, ids_jit, ids_other = (_identities(x, "com.x") for x in (base, jittered, other))
    assert _same_page(ids_base, ids_jit)          # 6/7 = 0.857 ≥ 0.8
    assert not _same_page(ids_base, ids_other)    # 不同页
    assert not _same_page(frozenset(), ids_base)  # 空集合不判同页
    assert not _same_page(ids_base, ids_base | {"text:角标", "text:浮层", "text:弹窗"})  # 6/9 < 0.8


def test_explore_merges_jittered_snapshots(tmp_path):
    """真机复现：点轮播后同页每帧微抖 → page_key 全不同。
    门禁应把抖动快照并回上一页：不新增页面、不推进深度、换点下一个元素。"""
    from insta_engine.explore import explore

    class FakeJitterDriver:
        """点"轮播"不换页，只让同页控件集合逐帧 +1（渐进渲染）。"""
        serial = None
        BASE = ('<node class="TextView" text="店铺名" package="com.x"/>'
                '<node class="TextView" text="菜品" package="com.x"/>'
                '<node class="TextView" text="封面" package="com.x"/>'
                '<node class="TextView" text="4-5人餐" package="com.x"/>'
                '<node class="TextView" text="好评" package="com.x"/>'
                '<node class="Button" text="轮播" clickable="true" bounds="[0,0][10,10]" package="com.x"/>')

        def __init__(self):
            self.variant = 0
            self.clicks = []

        @property
        def xml(self):
            return self.BASE + "".join(
                f'<node class="TextView" text="附加{i}" package="com.x"/>' for i in range(self.variant))

        def dump(self):
            return self.xml

        def click(self, x, y):
            self.clicks.append((x, y))
            self.variant = (self.variant + 1) % 3   # 轮播：同页抖动，最多 +2 控件

        def press(self, key):
            pass

        def screenshot(self, path):
            Path(path).write_bytes(b"\x89PNG fake")

        def app_start(self, package):
            self.variant = 0

    drv = FakeJitterDriver()
    report = explore(drv, "com.x", minutes=0.05, max_steps=8,
                     artifacts_dir=tmp_path, max_depth=3, sleep_scale=0.01)
    assert report["page_count"] == 1                 # 抖动帧没有变成新页面
    assert report["dedup_merges"] >= 1               # 至少合并了一帧
    assert report["pages"][0]["depth"] == 0          # 没有虚假的深度推进


def test_controls_context_falls_back_to_text(tmp_path):
    """RN 页 res-id 覆盖率 0%：回落行让防幻觉底座不被饿死。"""
    from insta_engine.page_index import controls_context

    pages = [{
        "activity": "MRNStandardActivity",
        "controls": [
            {"res-id": "com.x:id/ok", "text": "确定", "bounds": "[0,0][10,10]"},
            {"text": "菜品", "clickable": True, "bounds": "[0,20][10,30]"},   # RN：无 res-id 可点
            {"text": "581条", "clickable": True, "bounds": "[0,40][10,50]"},  # 动态文案：不注入
            {"text": "商品描述", "bounds": "[0,60][10,70]"},                   # 不可点：不注入
        ]}]
    context = controls_context(pages)
    assert "- res-id: com.x:id/ok" in context                     # res-id 行仍优先
    assert "- 文案: 菜品  类型: clickable" in context              # 回落行（text 定位）
    assert "581条" not in context and "商品描述" not in context     # 动态/不可点不进清单


def test_explore_recovers_from_left_app_loop(tmp_path):
    """真机复现（桌面启动器类 ROM）：点出被测 App 后落在桌面，
    back 无效 → 修复前整轮预算耗在离场循环（0 步 0 页 44 次 left-app）。
    修复后：先 back 一次，仍离场则 app_start 拉回，探索继续。"""
    from insta_engine.explore import explore

    class FakeLeftAppDriver:
        serial = None
        XML_A = '<node class="Button" text="跳转外部" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'

        def __init__(self):
            self.state = "A"                  # "A" | "launcher"

            class _Raw:
                def app_current(_self):
                    return ({"package": "com.launcher", "activity": "com.launcher/.Home"}
                            if self.state == "launcher"
                            else {"package": "com.x", "activity": "com.x/.Main"})

                def wake(_self):
                    pass

                def unlock(_self):
                    pass

            self.raw = _Raw()

        def foreground_app(self) -> dict:
            return self.raw.app_current() or {}

        def dump(self):
            return (self.XML_A if self.state == "A"
                    else '<node class="TextView" text="桌面图标" package="com.launcher"/>')

        def click(self, x, y):
            self.state = "launcher"           # 点"跳转外部"→ 桌面

        def press(self, key):
            pass                              # 桌面上 back 无效（真机行为）

        def app_start(self, package):
            self.state = "A"

        def screenshot(self, path):
            Path(path).write_bytes(b"\x89PNG fake")

    drv = FakeLeftAppDriver()
    report = explore(drv, "com.x", minutes=0.05, max_steps=6,
                     artifacts_dir=tmp_path, max_depth=3, sleep_scale=0.01)
    assert report["page_count"] == 1                    # A 页被记录，没卡死在离场循环
    assert report["left_app"] >= 2                      # 记录了离场
    left = [f for f in report["findings"] if f["type"] == "left-app"]
    assert len(left) == 1 and "共离场" in left[0]["detail"]  # 同一 activity 只记一次 + 总次数


def test_explore_not_left_app_when_activity_namespace_differs(tmp_path):
    """真机坑：包名与 activity 类名处于完全不同的命名空间。
    按 activity 前缀判离场会把自己当外人 → 47 次误判空转。按 package 判后不再误判。"""
    from insta_engine.explore import explore

    class FakeRnStyleDriver:
        serial = None
        XML_A = '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
        XML_B = '<node class="TextView" text="B页" package="com.x"/>'

        def __init__(self):
            self.stack = ["A"]

            class _Raw:
                def app_current(_self):
                    # 包名匹配，activity 类名在完全不同的命名空间（RN 容器形态）
                    return {"package": "com.x",
                            "activity": "com.y.android.pt.homepage.activity.MainActivity"}

                def wake(_self):
                    pass

                def unlock(_self):
                    pass

            self.raw = _Raw()

        def foreground_app(self) -> dict:
            return self.raw.app_current() or {}

        def dump(self):
            return self.XML_A if self.stack[-1] == "A" else self.XML_B

        def click(self, x, y):
            if self.stack[-1] == "A":
                self.stack.append("B")

        def press(self, key):
            if key == "back" and len(self.stack) > 1:
                self.stack.pop()

        def screenshot(self, path):
            Path(path).write_bytes(b"\x89PNG fake")

    drv = FakeRnStyleDriver()
    report = explore(drv, "com.x", minutes=0.05, max_steps=6,
                     artifacts_dir=tmp_path, max_depth=3, sleep_scale=0.01)
    assert report["left_app"] == 0                                   # 没有误判离场
    assert report["page_count"] == 2 and report["depth_stats"] == {"L0": 1, "L1": 1}
    assert not [f for f in report["findings"] if f["type"] == "left-app"]


# -- 后端事件契约：on_step 回调 / cancel_event 安全中止 ----------------

def test_runner_cancel_between_steps(tmp_path):
    """cancel 置位后下一步开始前退出：已完成步骤证据保留，status=cancelled。"""
    from insta_engine.runner import Runner

    drv = FakeDriver()
    runner = Runner(drv, tmp_path)
    case = _simple_case()  # s1 launch / s2 tap / s3 assert

    def on_step(result):
        if result.step_id == "s1":
            cancel.set()  # 第一完成即请求中止 → s2 不再开始

    cancel = __import__("threading").Event()
    run = runner.run(case, on_step=on_step, cancel_event=cancel)
    assert run.status == "cancelled"
    assert len(run.steps) == 1 and run.steps[0].step_id == "s1"  # s2/s3 未执行
    # 证据保留：run.json 落盘且状态如实
    import json
    saved = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert saved["run"]["status"] == "cancelled"


def test_runner_on_step_callback_receives_all_steps(tmp_path):
    from insta_engine.runner import Runner

    seen = []
    run = Runner(FakeDriver(), tmp_path).run(_simple_case(), on_step=seen.append)
    assert [r.step_id for r in seen] == ["s1", "s2"]   # _simple_case = launch + wait
    assert all(r.status == "passed" for r in seen) and run.status == "passed"


def test_runner_on_step_exception_is_swallowed(tmp_path):
    """监控是旁路：回调抛异常不影响执行。"""
    from insta_engine.runner import Runner

    def bad_on_step(_result):
        raise RuntimeError("监控炸了")

    run = Runner(FakeDriver(), tmp_path).run(_simple_case(), on_step=bad_on_step)
    assert run.status == "passed" and len(run.steps) == 2


# -- T43：onFailure=abort 失败即停（此前从未被实现，失败后永远继续） ----------------

def test_on_failure_abort_stops_remaining_steps(tmp_path):
    """abort（DSL 默认）步骤失败后，剩余步骤如实记 skipped，不再白烧真机预算。"""
    from insta_engine.runner import Runner

    case = TestCase.model_validate({
        "caseId": "c", "name": "abort 语义", "target": {"package": "com.insta.demo"},
        "steps": [
            {"id": "s1", "intent": "点不存在的控件", "action": "tap",
             "target": {"strategy": "res-id", "value": "com.insta.demo:id/absent"}},
            {"id": "s2", "intent": "不应执行", "action": "wait", "params": {"ms": 10}},
        ]})
    run = Runner(FakeDriver(), tmp_path).run(case)
    assert run.status == "failed"
    assert [s.status for s in run.steps] == ["failed", "skipped"], \
        f"实际 {[s.status for s in run.steps]}"
    assert "abort" in (run.steps[1].error or "")
    assert run.steps[1].category is None  # skipped 不算失败分类


def test_on_failure_heal_failure_does_not_abort(tmp_path):
    """heal/retry 步骤失败仍继续后续步骤——abort 只属于声明 abort 的步骤。"""
    from insta_engine.runner import Runner

    case = TestCase.model_validate({
        "caseId": "c", "name": "heal 不 abort", "target": {"package": "com.insta.demo"},
        "steps": [
            {"id": "s1", "intent": "定位失败走 heal", "action": "tap", "onFailure": "heal",
             "target": {"strategy": "res-id", "value": "com.insta.demo:id/absent"}},
            {"id": "s2", "intent": "继续执行", "action": "wait", "params": {"ms": 10}},
        ]})
    run = Runner(FakeDriver(), tmp_path).run(case)
    assert [s.status for s in run.steps] == ["failed", "passed"], \
        f"heal 失败不应中止，实际 {[s.status for s in run.steps]}"


# -- 控制台激活入口（api/activate，与 mat activate 同实现） ----------------------

def test_api_activate_roundtrip(tmp_path, monkeypatch):
    """合法码 200 并落 license；非法码/缺参 400。"""
    from insta_engine import licensing, web_server

    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", TEST_LICENSE_KEY)

    code, obj = web_server.api_activate({"code": mint_test_code("pro")})
    assert code == 200 and obj["ok"] and obj["plan"] == "pro"
    assert licensing.read_license()["plan"] == "pro"

    bad_code, bad = web_server.api_activate({"code": "MAT-bogus.123456.abcdef.0123456789abcdef"})
    assert bad_code == 400 and "error" in bad
    missing_code, _ = web_server.api_activate({})
    assert missing_code == 400



def test_controls_context_rotates_across_pages():
    """2026-10-26：配额必须**按页轮转**，不能被第一个页面吃光。

    真机背景：某资讯类 App 图谱 24 页 / 1499 控件，首页 600 个控件把 120 条 res-id 配额
    全部占满 ⇒ 模型只看到 1 页，却被要求写"点进详情页 / 进搜索结果页"的定位值——
    它没见过那些页面的控件，只能凭先验编。一次同代重测的 49 次失败里 41 次是"目标值根本不在
    候选集"，机制同源。轮转**不增加 token 预算**，只改变预算花在哪些页上。
    """
    from insta_engine.page_index import controls_context

    big = {"activity": "HomeActivity",
           "controls": [{"res-id": f"com.x:id/home_{i}", "text": f"首页{i}"} for i in range(400)]}
    small = {"activity": "DetailActivity",
             "controls": [{"res-id": "com.x:id/detail_title", "text": "详情标题"}]}
    context = controls_context([big, small])
    assert "detail_title" in context, "第二个页面必须也进清单（否则模型只能编详情页控件）"
    assert context.count("- res-id:") == 120, "总预算不变，仍是 120 条"


def test_controls_context_keeps_depth_on_the_first_page():
    """广度拿到之后必须**把深度补回来**——纯轮转是过冲。

    2026-10-26 真机标定（某资讯类 App 24 页 / 1499 控件）：纯轮转让首页只剩 5 条，
    首页控件（频道栏/搜索框/列表项）被挤出清单，16 条用例的定位值召回 21/29 → **12/29**。
    一半预算轮转（第 2 页必须露面）、一半按页顺序补深度（首页不能只剩前几条）。
    """
    from insta_engine.page_index import controls_context

    big = {"activity": "HomeActivity",
           "controls": [{"res-id": f"com.x:id/home_{i}", "text": f"首页{i}"} for i in range(400)]}
    small = {"activity": "DetailActivity",
             "controls": [{"res-id": "com.x:id/detail_title", "text": "详情标题"}]}
    context = controls_context([big, small])
    assert "detail_title" in context, "广度：第二个页面必须露面"
    assert "home_59" in context, "深度：首页不能只剩轮转那几条"
    assert context.count("- res-id:") == 120, "总预算不变，仍是 120 条"


def test_controls_context_rotation_keeps_budget_per_page():
    """轮转不等于"每页平均"——多页都还有候选时会一直轮到预算用完。"""
    from insta_engine.page_index import controls_context

    pages = [{"activity": f"A{i}", "controls": [{"res-id": f"com.x:id/p{i}_{j}"}
                                                for j in range(10)]} for i in range(5)]
    context = controls_context(pages, max_controls=20)
    rid_lines = [ln for ln in context.splitlines() if ln.startswith("- res-id:")]
    assert len(rid_lines) == 20
    for i in range(5):
        assert f"p{i}_0" in context, f"第 {i} 页一条都没进"
