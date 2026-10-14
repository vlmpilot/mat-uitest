"""手工用例导入测试：解析器、启发式映射、分级转换、配额 0.5 计费、批次落盘。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine.dsl import TestCase
from insta_engine.manual_import import (
    ManualCase,
    TemplateError,
    _clean_locator,
    _quality_issues,
    _sanitize_assertions,
    _sanitize_targets,
    _split_steps,
    convert_batch,
    convert_case,
    heuristic_template,
    parse_file,
    resolve_columns,
    save_import_bundle,
)

REPO = Path(__file__).resolve().parents[2]

CSV_CONTENT = """用例编号,用例标题,所属模块,优先级,前置条件,操作步骤,测试数据,预期结果
TC-001,登录成功,登录,P1,已安装App,"1. 输入账号
2. 输入密码
3. 点击登录",alice,进入首页
TC-002,搜索商品,搜索,P2,,1. 点搜索框
2. 输入蓝牙,,列表出现蓝牙耳机
"""


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# -- 启发式映射 -----------------------------------------------------------------

def test_heuristic_template_chinese_headers():
    template = heuristic_template(["用例编号", "用例标题", "所属模块", "优先级", "前置条件", "操作步骤", "测试数据", "预期结果"])
    assert template["title"] == 1
    assert template["steps_action"] == 5
    assert template["steps_expected"] == 7
    assert "external_id" in template


def test_heuristic_template_missing_required():
    with pytest.raises(TemplateError, match="必须列"):
        heuristic_template(["备注", "备注2"])


def test_resolve_columns_by_name():
    resolved = resolve_columns({"title": "标题", "steps_action": ["步骤"]}, ["序号", "标题", "步骤"])
    assert resolved == {"title": 1, "steps_action": [2]}


def test_split_steps_numbering():
    assert _split_steps("1. 输入账号\n2、输入密码\n3）点击") == ["输入账号", "输入密码", "点击"]


# -- 解析器 ---------------------------------------------------------------------

def test_parse_csv(tmp_path):
    cases, used = parse_file(_write(tmp_path / "cases.csv", CSV_CONTENT))
    assert used["title"] == 1 and used["steps_action"] == 5
    assert cases[0].source["template"] == "heuristic" and cases[0].source["row"] == 2
    assert len(cases) == 2
    first = cases[0]
    assert first.title == "登录成功" and first.module == "登录" and first.priority == "P1"
    assert [s.action for s in first.steps] == ["输入账号", "输入密码", "点击登录"]
    assert first.steps[0].data == "alice" and first.expected_result == "进入首页"
    assert first.steps[2].expected is None  # 单条预期归入整体预期，由 AI 拆解为末步断言
    assert first.source["row"] == 2 and first.source["format"] == "csv"
    assert cases[1].steps[0].expected is None


def test_parse_csv_custom_template(tmp_path):
    template = {"title": "用例标题", "steps_action": ["操作步骤"]}
    cases, used = parse_file(_write(tmp_path / "c.csv", CSV_CONTENT), template)
    assert used == template
    assert cases[0].steps[0].action == "输入账号"


def test_parse_markdown_table(tmp_path):
    md = "| 用例标题 | 操作步骤 | 预期结果 |\n|---|---|---|\n| 登录 | 1. 打开 2. 点登录 | 进入首页 |\n"
    cases, _ = parse_file(_write(tmp_path / "c.md", md))
    assert len(cases) == 1 and [s.action for s in cases[0].steps] == ["打开", "点登录"]


def test_parse_markdown_br_becomes_newline(tmp_path):
    """回归（2026-10-11）：markdown 表格单元格里的 `<br>` 必须还原成换行。

    修复前的实测症状：`1. 第一步<br>2. 第二步` 被解析成 **1 步**（action 里带着 `<br>`）。
    根因是 `_split_steps` 的行内编号正则要求编号前是**空白**（防误切「3.5 秒」），
    而 `<br>` 后是 `>` ⇒ 不切。markdown 表格里换行只能写 `<br>`（真换行会切断表格），
    所以这是**最常见**的写法 —— 而它**不报错**，只是把整列并成 1 步。
    """
    md = ("| 用例标题 | 操作步骤 | 预期结果 |\n"
          "|---|---|---|\n"
          "| 甲 | 1. 第一步<br>2. 第二步 | 1. 预期A<br>2. 预期B |\n")
    cases, _ = parse_file(_write(tmp_path / "m.md", md))
    assert len(cases) == 1
    assert [s.action for s in cases[0].steps] == ["第一步", "第二步"], \
        f"应切出 2 步，实为 {[s.action for s in cases[0].steps]}"
    assert [s.expected for s in cases[0].steps] == ["预期A", "预期B"]


def test_parse_text_blocks(tmp_path):
    txt = "标题: 登录成功\n前置: 已安装App\n步骤: 1. 输入账号\n2. 点击登录\n预期: 进入首页\n\n标题: 登出\n步骤: 点击退出\n"
    cases, _ = parse_file(_write(tmp_path / "c.txt", txt))
    assert [c.title for c in cases] == ["登录成功", "登出"]
    assert len(cases[0].steps) == 2 and cases[0].preconditions == ["已安装App"]


def test_parse_text_multiline_fields_go_to_their_own_label(tmp_path):
    """回归（2026-10-11）：**多行值必须归给「最近那个标签」**，不能一律塞进 `操作步骤`。

    修复前的实测症状（最小复现）：写 2 步 + 2 行测试数据 + 2 行预期 ⇒ 解析出 **4 步**，
    后 2 步其实是「数据B」「预期B」被当成了操作；且 `data` 全是 None。
    ⚠️ 它**不报错** —— 解析照常返回用例，只是内容错了（多步骤 / 数据丢失），
    与台账 F14「代理判据冒充证据」同族。
    """
    txt = ("用例标题: 多行字段\n"
           "操作步骤: 1. 第一步\n2. 第二步\n"
           "测试数据: 1. 数据A\n2. 数据B\n"
           "预期结果: 1. 预期A\n2. 预期B\n")
    cases, _ = parse_file(_write(tmp_path / "m.txt", txt))
    assert len(cases) == 1
    c = cases[0]
    assert [s.action for s in c.steps] == ["第一步", "第二步"], \
        f"只应有 2 步；多出来的步骤说明续行又被塞进操作步骤了：{[s.action for s in c.steps]}"
    assert [s.data for s in c.steps] == ["数据A", "数据B"], \
        f"测试数据必须按行配对（修复前恒为 None）：{[s.data for s in c.steps]}"
    assert [s.expected for s in c.steps] == ["预期A", "预期B"], \
        f"预期必须按行配对：{[s.expected for s in c.steps]}"


def test_parse_text_continuation_still_works_for_steps(tmp_path):
    """向后兼容：紧跟 `操作步骤` 的续行仍进操作步骤（修复前唯一的可用路径）。"""
    txt = "标题: 登录\n步骤: 1. 输入账号\n2. 点击登录\n3. 断言首页\n"
    cases, _ = parse_file(_write(tmp_path / "c.txt", txt))
    assert [s.action for s in cases[0].steps] == ["输入账号", "点击登录", "断言首页"]


def test_parse_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["用例标题", "操作步骤", "预期结果"])
    sheet.append(["登录成功", "1. 输入账号\n2. 点击登录", "进入首页"])
    path = tmp_path / "cases.xlsx"
    workbook.save(path)
    cases, _ = parse_file(path)
    assert len(cases) == 1 and cases[0].steps[1].action == "点击登录"
    assert cases[0].source["format"] == "xlsx"


def test_unsupported_format(tmp_path):
    with pytest.raises(TemplateError, match="不支持"):
        parse_file(_write(tmp_path / "c.doc", "x"))


def test_parse_xmind_zen(tmp_path):
    import json as _json
    import zipfile
    step3 = {"title": "点击登录"}
    step2 = {"title": "输入密码", "children": {"attached": [{"title": "进入首页"}]}}
    step1 = {"title": "输入账号"}
    content = [{
        "title": "登录模块",
        "rootTopic": {"title": "登录测试", "children": {"attached": [
            {"title": "登录成功", "children": {"attached": [step1, step2, step3]}},
            {"title": "搜索商品", "children": {"attached": [{"title": "点搜索框"}]}},
        ]}},
    }]
    path = tmp_path / "c.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", _json.dumps(content, ensure_ascii=False))
    cases, used = parse_file(path)
    assert used["mode"] == "xmind" and len(cases) == 2
    assert cases[0].module == "登录模块" and len(cases[0].steps) == 3
    assert cases[0].steps[1].expected == "进入首页"


def test_parse_xmind_legacy(tmp_path):
    import zipfile
    xml = """<sheet><topic><title>地址模块</title><children><topics>
      <topic><title>新增地址</title><children><topics>
        <topic><title>点击新增</title></topic></topics></children></topic>
      </topics></children></topic></sheet>"""
    path = tmp_path / "c.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", xml)
    cases, _ = parse_file(path)
    assert len(cases) == 1 and cases[0].title == "新增地址" and cases[0].module == "地址模块"


def test_parse_xmind_preconditions(tmp_path):
    """用例标题下「前置[:：]」开头的子主题拆入 preconditions，不作为步骤（发起人 2026-10-12）。"""
    import json as _json
    import zipfile
    content = [{
        "title": "搜索模块",
        "rootTopic": {"title": "搜索测试", "children": {"attached": [
            {"title": "搜索蓝牙", "children": {"attached": [
                {"title": "前置：已进入搜索结果页"},
                {"title": "前置", "children": {"attached": [
                    {"title": "已登录账号"},
                    {"title": "蓝牙已开启"},
                ]}},
                {"title": "点搜索框"},
                {"title": "输入蓝牙", "children": {"attached": [{"title": "列表出现蓝牙耳机"}]}},
            ]}},
        ]}},
    }]
    path = tmp_path / "c.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", _json.dumps(content, ensure_ascii=False))
    cases, _ = parse_file(path)
    assert len(cases) == 1
    case = cases[0]
    assert case.preconditions == ["已进入搜索结果页", "已登录账号", "蓝牙已开启"]
    assert [s.action for s in case.steps] == ["点搜索框", "输入蓝牙"]
    assert case.steps[1].expected == "列表出现蓝牙耳机"


# -- AI 转换与分级（FakeClient，无网络） -----------------------------------------

class FakeClient:
    def __init__(self, responses: list[dict]):
        self._responses = list(responses)

    def complete_raw(self, messages, context=""):
        return self._responses.pop(0)


@pytest.fixture()
def prompt_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "convert.prompt").write_text("IR: {ir} APP: {app_info}", encoding="utf-8")
    return tmp_path


def _ir() -> ManualCase:
    return ManualCase.model_validate({
        "title": "登录成功", "module": "登录", "priority": "P1", "tags": ["冒烟"],
        "steps": [{"index": 1, "action": "输入账号", "data": "alice"},
                  {"index": 2, "action": "点击登录", "expected": "进入首页"}],
        "source": {"format": "csv", "file": "c.csv", "row": 2},
    })


def _valid_dsl_payload(confidence: float, issues: list[str] | None = None) -> dict:
    """一份**真实形状**的转换产物：首步 launch（否则"起始状态无人保证"会被质量闸门报出来）。

    ⚠️ 夹具曾经没有 launch（`输入账号 → 点击登录`）—— 那正是**真实缺陷的缩影**：
    实测 37 条失败里 13 条没有 launch 步骤。闸门补上这条判据后夹具被正确地报了出来，
    所以改的是**夹具**（让它贴近真实用例），不是判据。
    """
    return {"confidence": confidence, "issues": issues or [],
            "case": {"caseId": "case_imp1", "name": "登录成功", "target": {"package": "com.insta.demo"},
                     "vars": {"账号": "alice"},
                     "steps": [{"id": "s0", "intent": "启动 App", "action": "launch",
                                "params": {"package": "com.insta.demo"}},
                               {"id": "s1", "intent": "输入账号", "action": "input", "sourceStep": 1,
                                "target": {"strategy": "text", "value": "账号"}, "params": {"text": "${账号}"}},
                               {"id": "s2", "intent": "点击登录", "action": "tap", "sourceStep": 2,
                                "target": {"strategy": "text", "value": "登录"},
                                "assertions": [{"type": "exists", "target": {"strategy": "text", "value": "首页"}}]}]}}


def _launch_step() -> dict:
    """最小夹具的 launch 首步。

    没有它，「起始状态无人保证」会被质量闸门**正确地**报出来 ——
    凡是断言"闸门零 issue"的夹具都必须带上它。
    """
    return {"id": "s0", "action": "launch", "params": {"package": "com.insta.demo"}}


def _step_of(payload: dict, keyword: str) -> dict:
    """按 intent 关键词取步骤 —— **不写下标**。

    下标写法在夹具增删步骤时会**静默指错步骤**（本次给夹具补 launch 首步时，
    `steps[1]` 从"点击登录"变成了"输入账号"，测试照样能跑、断言却测错了对象）。
    """
    hit = [s for s in payload["case"]["steps"] if keyword in str(s.get("intent", ""))]
    assert len(hit) == 1, f"intent 含「{keyword}」的步骤应恰好 1 个，实际 {len(hit)}"
    return hit[0]


def test_convert_grading(tmp_path, prompt_file):
    """分级**只看确定性判据**（严重度），不看模型自评 —— 这是 R14 的核心。"""
    high = convert_case(FakeClient([_valid_dsl_payload(0.92)]), _ir(), "com.insta.demo")
    assert high["grade"] == "A" and high["case_file"]
    assert high["dsl"]["meta"]["module"] == "登录" and high["dsl"]["meta"]["tags"] == ["冒烟"]
    # 首步必须是 launch（"起始状态由谁保证"是 A 级用例的硬要求），sourceStep 按位置重排
    assert high["dsl"]["steps"][0]["action"] == "launch"
    assert high["dsl"]["steps"][0]["sourceStep"] == 1

    # ⚠️ 自评 0.65：**零缺陷 ⇒ 仍然是 A**。自评是模型自己的判断，不可验证，
    #    拿它当质量信号就是代理指标（同族 D10/F14）。它只作为提示（见下一条）。
    middle = convert_case(FakeClient([_valid_dsl_payload(0.65)]), _ir(), "com.insta.demo")
    assert middle["grade"] == "A", "自评不该参与分级"
    assert middle["confidence"] == 0.65, "但自评要**如实输出**，供人参考"

    # 自评 < 0.5 ⇒ 记一条 R14 提示（等级落到 B），而不是直接判 C
    low = convert_case(FakeClient([_valid_dsl_payload(0.3)]), _ir(), "com.insta.demo")
    assert low["grade"] == "B"
    assert any(i.startswith("[R14]") for i in low["issues"]), low["issues"]
    assert low["case_file"], "低自评**照样给草稿**（草稿是给人改的，不给等于扔掉已花的额度）"

    broken = convert_case(FakeClient([{"confidence": 0.9, "case": {"caseId": "x", "steps": []}}]),
                          _ir(), "com.insta.demo")
    assert broken["grade"] == "C" and any(i.startswith("[X0]") for i in broken["issues"]), broken["issues"]


def test_convert_no_assertion_downgrades(tmp_path, prompt_file):
    payload = _valid_dsl_payload(0.92)
    _step_of(payload, "点击登录")["assertions"] = []
    record = convert_case(FakeClient([payload]), _ir(), "com.insta.demo")
    assert record["grade"] == "B" and any("断言" in i for i in record["issues"])


def test_convert_keeps_case_owned_launch(tmp_path, prompt_file):
    """自身以 `launch` 开头的用例**不再被补第二个 launch**（冷启动语义由用例自持）。

    ⚠️ 原测试断言 console 分支那套"补冷启动前置"的专有字段 —— 那套实现已在合并时
    去重（= main 的 `ensure_launch` / R6），字段随之消失。这里保留**真正有价值的那半句**：
    不能补出第二个 launch，且用例自己的 params 要保住。
    """
    payload = _valid_dsl_payload(0.92)
    payload["case"]["steps"][0] = {"id": "s0", "intent": "冷启动应用", "action": "launch",
                                   "sourceStep": 1, "params": {"package": "com.insta.demo", "cold": True}}
    record = convert_case(FakeClient([payload]), _ir(), "com.insta.demo")
    launches = [s for s in record["dsl"]["steps"] if s["action"] == "launch"]
    assert len(launches) == 1, "不该补出第二个 launch"
    assert launches[0]["params"] == {"package": "com.insta.demo", "cold": True}, "用例自己的参数要保留"


def test_convert_assertion_target_inherited(tmp_path, prompt_file):
    """断言缺 target 且本步有定位 ⇒ 继承本步 target 并注记（**端到端接线**）。"""
    payload = _valid_dsl_payload(0.92)
    # ⚠️ 挂到 **input** 步骤：若挂到 `tap(登录)` 上，继承后断言目标 == 本步目标
    # ⇒ 会命中 **R2 导航耦合**（BLOCK，等级 C）——那是另一条判据的功劳，不是本条要测的
    _step_of(payload, "输入账号")["assertions"] = [{"type": "exists", "target": None}]
    record = convert_case(FakeClient([payload]), _ir(), "com.insta.demo")
    assert record["grade"] == "B" and any("已继承本步操作目标" in i for i in record["issues"])
    s1 = next(s for s in record["dsl"]["steps"] if s["id"] == "s1")
    t = s1["assertions"][0]["target"]
    assert (t["strategy"], t["value"]) == ("text", "账号")


def test_convert_assertion_target_dropped(tmp_path, prompt_file):
    """断言缺 target 且本步无定位可继承（input/launch/wait）⇒ 删除 + 记 issue。"""
    payload = _valid_dsl_payload(0.92)
    step = _step_of(payload, "输入账号")
    step["assertions"] = [{"type": "text-contains", "target": None, "expected": "进入首页"}]
    step["target"] = None
    record = convert_case(FakeClient([payload]), _ir(), "com.insta.demo")
    assert record["grade"] != "A" and any("删除" in i and "断言" in i for i in record["issues"])
    s1 = next(s for s in record["dsl"]["steps"] if s["id"] == "s1")
    assert s1["assertions"] == []


def test_convert_strips_a11y_annotation(tmp_path, prompt_file):
    """转换侧自动剥离无障碍注解（**R5**）—— 实测：XMind「视频，可选中」原样转成 text 定位必挂。

    ⚠️ 原测试还断言"审计警告把置信度封顶（A→B）"—— 那条口径已在合并时改成
    **确定性分级**（R14：自评不参与、只有 WARN/BLOCK 才降级；R5 是 INFO 不降级），
    所以这里只锁"注解被剥离"这个真正防挂的行为。
    """
    idx = tmp_path / "artifacts" / "page_index"
    idx.mkdir(parents=True)
    (idx / "com.insta.demo.json").write_text(json.dumps({
        "pages": [{"depth": 0, "activity": ".MainActivity", "controls": [
            {"text": "账号", "clickable": True},
            {"text": "登录", "clickable": True},
            {"text": "首页", "clickable": True},
            {"text": "视频", "clickable": True},
        ]}]}, ensure_ascii=False), encoding="utf-8")
    payload = _valid_dsl_payload(0.92)
    _step_of(payload, "点击登录")["target"] = {"strategy": "text", "value": "视频，可选中"}
    record = convert_case(FakeClient([payload]), _ir(), "com.insta.demo", index_dir=idx)
    assert any("状态后缀" in i for i in record["issues"]), record["issues"]
    s2 = next(s for s in record["dsl"]["steps"] if s["id"] == "s2")
    assert s2["target"]["value"] == "视频", "转换侧应自动剥离注解"


def test_convert_batch_quota_and_bundle(tmp_path, prompt_file, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    from insta_engine.quota import UsageStore

    usage = UsageStore(tmp_path / "home" / "usage.json")
    cases = [_ir(), _ir(), _ir()]
    client = FakeClient([_valid_dsl_payload(0.9), _valid_dsl_payload(0.6),
                         {"confidence": 0.2, "case": {"bad": 1}}])
    batch = convert_batch(client, cases, "com.insta.demo", usage)
    # 分级只看确定性判据 ⇒ 0.9 与 0.6 都是 A（零缺陷），坏的第三条是 C
    assert batch["summary"] == {"total": 3, "A": 2, "B": 0, "C": 1}
    # 3 条 × 0.5 = 1.5 次额度
    assert json.loads((tmp_path / "home" / "usage.json").read_text())["gen_count"] == 1.5
    # R15：批次带可复算的质量口径（**不是**"可执行率"，见 quality_stats 的说明）
    quality = batch["quality"]
    assert quality["total"] == 3 and quality["blocked"] == 1
    assert quality["no_blocking_rate"] == round(2 / 3, 3)
    assert quality["by_rule"].get("X0") == 1

    source = _write(tmp_path / "cases.csv", CSV_CONTENT)
    import_id, bundle = save_import_bundle(tmp_path, source, "com.insta.demo", batch, {"title": 1})
    report = json.loads((bundle / "import.json").read_text(encoding="utf-8"))
    assert report["import_id"] == import_id and report["summary"]["total"] == 3
    assert (bundle / "source.csv").exists()
    drafts = list((tmp_path / "cases").glob("import-*.json"))
    assert len(drafts) == 2  # 只有 A/B 写草稿


def test_quota_half_step_insufficient(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    from insta_engine.quota import FREE_GEN_LIMIT, QuotaExceeded, UsageStore

    store = UsageStore(tmp_path / "usage.json")
    for _ in range(FREE_GEN_LIMIT):  # 体验版 50 次/月；整额耗尽后半次也不放行
        store.check_gen()
    with pytest.raises(QuotaExceeded, match="剩 0"):
        store.ensure_capacity(0.5)


# -- DSL 兼容性（meta/sourceStep 不破坏旧用例） -----------------------------------

def test_dsl_meta_and_source_step_roundtrip():
    case = TestCase.model_validate({
        "caseId": "case_m", "meta": {"module": "登录", "tags": ["冒烟"]},
        "steps": [{"id": "s1", "action": "tap", "sourceStep": 3,
                   "target": {"strategy": "text", "value": "登录"}}],
    })
    from insta_engine.dsl import dump_dsl
    raw = json.loads(dump_dsl(case))
    assert raw["steps"][0]["sourceStep"] == 3 and raw["meta"]["module"] == "登录"
    again = TestCase.model_validate(raw)
    assert again.steps[0].source_step == 3 and again.meta["tags"] == ["冒烟"]


def test_benchmark_gold_cases_still_valid():
    suite = json.loads((REPO / "benchmarks" / "tasks" / "spike-5-tasks.json").read_text(encoding="utf-8"))
    for task in suite["tasks"]:
        assert TestCase.model_validate(task["case"]).steps


# -- 断言消毒 / 质量闸门 / XMind 前置条件（2026-10-12）----------------------------
#
# 起因是实测：某资讯类客户端 16 条手工用例跑出 15 红，其中
#   · 5 条是 `AssertionError: 断言 exists 缺少 target`（导入产物里断言**没有定位**）
#   · 4 条是断言与导航耦合（点完入口按钮就没了，却还断言它存在）
#   · 若干条的定位文案是"某次运行的内容快照"（具体新闻标题 / 播客单集名）
# 这些本该在**导入阶段**被处理掉或标出来，而不是留到运行期变成一条看不懂的失败。


def test_assertion_without_target_inherits_the_step_target():
    raw = {"steps": [{"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "返回"},
                      "assertions": [{"type": "exists", "target": None}]}]}
    issues = _sanitize_assertions(raw)
    assert raw["steps"][0]["assertions"][0]["target"] == {"strategy": "text", "value": "返回"}
    assert any("继承本步操作目标" in i for i in issues)


def test_assertion_without_target_and_without_anchor_is_dropped():
    """**宁缺毋假**：没有定位的断言留着必然失败（`断言 exists 缺少 target`）。"""
    raw = {"steps": [{"id": "s1", "action": "wait", "target": None,
                      "assertions": [{"type": "exists", "target": None}]}]}
    issues = _sanitize_assertions(raw)
    assert raw["steps"][0]["assertions"] == []
    assert any("删除" in i for i in issues)


def test_valid_assertion_is_untouched():
    raw = {"steps": [{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "登录"},
                      "assertions": [{"type": "exists",
                                      "target": {"strategy": "text", "value": "首页"}}]}]}
    assert _sanitize_assertions(raw) == []
    assert raw["steps"][0]["assertions"][0]["target"]["value"] == "首页"


def test_state_suffix_is_stripped_from_step_and_assertion():
    """`视频，可选中` 里的「，可选中」是**控件状态**，不是文案 —— 状态一变就定位不到。"""
    raw = {"steps": [{"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "视频，可选中"},
                      "assertions": [{"type": "exists",
                                      "target": {"strategy": "text", "value": "新闻妹 AI，可选中"}}]}]}
    _sanitize_targets(raw)
    step = raw["steps"][0]
    assert step["target"]["value"] == "视频"
    assert step["assertions"][0]["target"]["value"] == "新闻妹 AI"


def test_state_suffix_strip_only_applies_to_text_strategies():
    """只对**文案型**策略剥离；res-id 里出现「，」也不该被动。"""
    assert _clean_locator({"strategy": "res-id", "value": "a:id，可选中"})["value"] == "a:id，可选中"


def test_quality_flags_content_snapshot():
    raw = {"steps": [{"id": "s1", "action": "tap",
                      "target": {"strategy": "text",
                                 "value": "点“经”之笔丨“六网协同”如何打开内需新空间"}}]}
    assert any("内容快照" in i for i in _quality_issues(raw))


def test_quality_does_not_flag_a_short_control_text():
    raw = {"steps": [_launch_step(), {"id": "s1", "action": "tap",
                                      "target": {"strategy": "text", "value": "早晚报"}}]}
    assert _quality_issues(raw) == []


def test_quality_flags_nav_coupled_assertion():
    """`tap(X)` 之后又断言 X 存在 ⇒ 点完页面变了、入口控件不在 ⇒ 必失败。"""
    raw = {"steps": [{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "早晚报"},
                      "assertions": [{"type": "exists",
                                      "target": {"strategy": "text", "value": "早晚报"}}]}]}
    assert any("导航耦合" in i for i in _quality_issues(raw))


def test_quality_does_not_flag_not_exists_after_tap():
    """`not-exists` 是**正确**写法（"点跳过 → 跳过按钮应当消失"），不许误报。"""
    raw = {"steps": [_launch_step(),
                     {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "跳过"},
                      "assertions": [{"type": "not-exists",
                                      "target": {"strategy": "text", "value": "跳过"}}]}]}
    assert _quality_issues(raw) == []


# -- 新增判据（2026-10-13）：不可见字符 / 未切 App / 起始状态无人保证 ------------------
#
# 依据：对 52 条历史跑测的 37 条失败逐条归因（内嵌用例 + 失败步报错 + "当前页面可见控件"现场），
# 发现这些缺陷类**确定性地必失败**，而当时的闸门一条都没报。判据清单见 `case_quality.py`。

def test_quality_flags_invisible_locator_text():
    """图标字体的**私有区字形**：人看着是空白，机器也匹配不到。

    实测 `{"strategy":"text","value":"\\ue917"}`（分享图标）跑出来是 `text= 未命中` ——
    光看报错会误判成"漏写文案"，真因是**不可见字符**。
    """
    raw = {"steps": [_launch_step(), {"id": "s1", "action": "tap",
                                      "target": {"strategy": "text", "value": "\ue917"}}]}
    issues = _quality_issues(raw)
    assert any("不可见字符" in i for i in issues), issues


def test_quality_flags_blank_locator_text():
    raw = {"steps": [_launch_step(), {"id": "s1", "action": "tap",
                                      "target": {"strategy": "text", "value": "   "}}]}
    assert any("定位文案为空" in i for i in _quality_issues(raw))


def test_quality_flags_missing_launch_step():
    """没 launch ⇒ 设备停在哪个 App/页面**无人保证**（实测 13/37 条失败属这类）。"""
    raw = {"steps": [{"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "搜索框"}}]}
    issues = _quality_issues(raw)
    assert any("没有 `launch` 步骤" in i for i in issues), issues


def test_quality_flags_launch_without_package():
    """`launch` 缺 `params.package` ⇒ 不会切到被测对象（实测 3 条用例整条跑在别的 App 上）。"""
    raw = {"steps": [{"id": "s0", "action": "launch", "params": {}},
                     {"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "搜索框"}}]}
    assert any("缺 `params.package`" in i for i in _quality_issues(raw))


def test_quality_flags_midflow_start_without_preconditions():
    """有 launch 但**不在首位**、且没声明前置条件 ⇒ 起始状态依然无人保证。"""
    raw = {"steps": [{"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "清空"}},
                     {"id": "s0", "action": "launch", "params": {"package": "com.insta.demo"}}]}
    issues = _quality_issues(raw)
    assert any("首步不是 `launch`" in i for i in issues), issues


def test_quality_flags_declared_precondition_without_provisioning():
    """⚠️ **声明了前置条件 ≠ 有人会去置备它** —— 照报，但消息要指向真正的缺口。

    实测依据：xlsx 批次 8 条用例声明了「已进入搜索结果页」等前置条件，
    跑测**全部照红**（locate-fail）—— 因为 MAT 的动作集改不了页面状态，
    前置条件只能靠人在执行前手工置备（或展开成导航步骤）。
    所以这里不能因为"声明了"就放行，否则闸门会把一个真实缺口当成已处理
    （同族 F14：代理判据冒充证据）。
    """
    raw = {"steps": [{"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "清空"}}]}
    issues = _quality_issues(raw, preconditions=["已进入搜索页"])
    assert any("可置备的页面状态" in i for i in issues), issues
    assert any("已进入搜索页" in i for i in issues), "消息里要带上具体是哪条前置条件"


def test_quality_does_not_flag_environment_precondition():
    """⚠️ 反向用例：**环境前提**（`App 已安装`）不许报 —— 它是运行前提，拦它会误伤全部用例。

    实测 xlsx 批次 16/16 条都带「App 已安装」⇒ 报它等于把闸门变成噪音。
    """
    raw = {"steps": [{"id": "s0", "action": "launch", "params": {"package": "com.insta.demo"}},
                     {"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "要闻"}}]}
    assert _quality_issues(raw, preconditions=["App 已安装", "已完成首次隐私授权"]) == []


def test_quality_flags_nav_coupled_assertion_for_resource_id():
    """导航耦合**与策略无关**：`tap(res-id)` 后断言同一 res-id 存在，一样必失败。

    ⚠️ 旧实现把这条判据限定在文案型策略上（`and is_text`），res-id 的漏网 ——
    实测 `首页进入 7x24 直播` 就是这样红的（`tap(infinite_24hour…)` + 断言它存在）。
    """
    raw = {"steps": [_launch_step(),
                     {"id": "s1", "action": "tap",
                      "target": {"strategy": "res-id", "value": "com.a:id/infinite_24hour"},
                      "assertions": [{"type": "exists",
                                      "target": {"strategy": "res-id",
                                                 "value": "com.a:id/infinite_24hour"}}]}]}
    assert any("导航耦合" in i for i in _quality_issues(raw))


def test_review_is_the_single_entry_for_both_paths():
    """`review()` = 消毒 + 检查**一站式** —— 产用例的两条路径都该走它。

    为什么要有这个入口：让调用方"自己挑几个子函数调"就是漂移的起点
    （`mat gen` 当初一道闸都没有，正是因为没有这样一个入口）。
    """
    from insta_engine.case_quality import review

    raw = {"steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}},
                     {"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "视频，可选中"},
                      "assertions": [{"type": "exists", "target": None}]}]}
    issues = review(raw)
    # 消毒生效：状态后缀被剥掉、空定位断言继承本步目标
    assert raw["steps"][1]["target"]["value"] == "视频"
    assert raw["steps"][1]["assertions"][0]["target"]["value"] == "视频"
    assert any("继承本步操作目标" in i for i in issues)


def test_convert_case_carries_preconditions_and_fixes_targetless_assertion(prompt_file):
    """端到端：IR 的前置条件进 DSL；模型给的空定位断言**不许**留在产物里。"""
    ir = _ir()
    ir.preconditions = ["已进入资讯详情页"]
    payload = _valid_dsl_payload(0.92)
    _step_of(payload, "点击登录")["assertions"] = [{"type": "exists", "target": None}]
    record = convert_case(FakeClient([payload]), ir, "com.insta.demo")

    assert record["dsl"]["preconditions"] == ["已进入资讯详情页"]
    produced = [a for s in record["dsl"]["steps"] for a in (s.get("assertions") or [])]
    assert produced and all(a.get("target") for a in produced), f"不该留下空定位断言：{produced}"
    assert any("继承本步操作目标" in i for i in record["issues"])


def test_convert_case_drops_assertion_when_no_anchor(prompt_file):
    """连"可继承的锚"都没有 ⇒ 删掉并记 issue，而不是让它在运行期必然失败。"""
    payload = _valid_dsl_payload(0.92)
    anchorless = _step_of(payload, "输入账号")
    anchorless["target"] = None
    anchorless["assertions"] = [{"type": "exists", "target": None}]
    record = convert_case(FakeClient([payload]), _ir(), "com.insta.demo")

    produced = [a for s in record["dsl"]["steps"] for a in (s.get("assertions") or [])]
    assert all(a.get("target") for a in produced)
    assert any("删除" in i for i in record["issues"])


def test_xmind_extracts_preconditions(tmp_path):
    """XMind 路径此前**一条前置条件都提不出来**（实测 `preconditions: []`），
    而 xlsx 路径能提 ⇒ 同一批用例换个源格式质量差一档。"""
    import zipfile

    content = json.dumps([{
        "title": "启动与授权",
        "rootTopic": {"title": "某资讯客户端", "children": {"attached": [
            {"title": "位置授权弹窗-点击同意", "children": {"attached": [
                {"title": "前置条件：位置权限尚未授予"},
                {"title": "启动 App"},
            ]}},
        ]}},
    }], ensure_ascii=False)
    path = tmp_path / "t.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", content)

    cases, _ = parse_file(path)

    assert len(cases) == 1
    assert cases[0].preconditions == ["位置权限尚未授予"]
    assert [s.action for s in cases[0].steps] == ["启动 App"], "前置条件不该被当成步骤"
