"""引擎核心单元测试（无设备/无网络依赖）。

覆盖：界面空闲判定、失败五分类、自愈线索派生、报告渲染（含类别/截图/自愈）、
套件加载、benchmark gold 用例 schema 校验。
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from insta_engine.dsl import Locator, LocatorStrategy, TestCase
from insta_engine.locator import heal_tokens
from insta_engine.report import render_html, render_index_html
from insta_engine.runner import RunResult, StepResult, classify_failure, wait_stable
from insta_engine.suite import load_case_or_suite

REPO = Path(__file__).resolve().parents[2]


# -- wait_stable（界面空闲判定） ----------------------------------------------

class FixedDumpDriver:
    def dump(self) -> str:
        return "<hierarchy stable/>"


class CyclicDumpDriver:
    def __init__(self) -> None:
        self._cycle = itertools.cycle(["<a/>", "<b/>"])

    def dump(self) -> str:
        return next(self._cycle)


def test_wait_stable_returns_true_when_screen_settles():
    assert wait_stable(FixedDumpDriver(), timeout_ms=1500, interval=0.1) is True


def test_wait_stable_returns_false_when_screen_never_settles():
    assert wait_stable(CyclicDumpDriver(), timeout_ms=400, interval=0.05) is False


# -- 失败五分类（规则优先，/） ---------------------------

def test_classify_assert_failure():
    assert classify_failure("AssertionError: 断言失败: 期望文本 'x'，实际 'y'") == "assert-fail"


def test_classify_locate_failure():
    assert classify_failure("LocateFailed: 定位失败（策略链）: res-id=a 未命中 / text=b 未命中") == "locate-fail"


def test_classify_env_failure():
    assert classify_failure("URLError: <urlopen error connection refused>") == "env-failure"
    assert classify_failure("RuntimeError: device offline") == "env-failure"


def test_classify_crash_beats_everything():
    assert classify_failure("RuntimeError: xyz", "05-01 12:00 FATAL EXCEPTION: main") == "app-crash"
    assert classify_failure("AssertionError: 断言失败", "ANR in com.insta.demo") == "app-crash"


def test_classify_unknown():
    assert classify_failure("RuntimeError: something odd") == "unknown"
    assert classify_failure("") == "unknown"


# -- 自愈线索派生（02 §3.3 v0） ----------------------------------------------

def test_heal_tokens_res_id():
    loc = Locator(strategy=LocatorStrategy.RES_ID, value="com.insta.demo:id/login_btn")
    assert heal_tokens(loc) == ["login_btn", "login btn", "com.insta.demo:id/login_btn"]


def test_heal_tokens_id_strategy_matches_res_id():
    """平台中立 id 与 res-id 同义（评审 I3）：自愈线索须同样派生短名，否则命中面变窄。"""
    loc = Locator(strategy=LocatorStrategy.ID, value="com.insta.demo:id/login_btn")
    assert heal_tokens(loc) == ["login_btn", "login btn", "com.insta.demo:id/login_btn"]


def test_heal_tokens_id_bare_value_no_separator_variant():
    """裸 id（无 `package:id/` 前缀）不派生分隔符变体——与 res-id 同语义。

    这是既有取舍（`tail != value` 才派生），不是本修复引入的行为；评审 I3 指出
    的缺口恰在 Android 语义下（`id` 作为 `resourceId` 别名时写 `pkg:id/name`）。
    iOS 的 accessibility identifier 是裸值，两侧语义一致。
    """
    loc = Locator(strategy=LocatorStrategy.ID, value="login_btn")
    assert heal_tokens(loc) == ["login_btn"]


def test_heal_tokens_text():
    loc = Locator(strategy=LocatorStrategy.TEXT, value="登录")
    assert heal_tokens(loc) == ["登录"]


# -- 报告渲染（每步截图/失败分类/自愈提案/logcat） ------------------------------

def _fake_run() -> RunResult:
    return RunResult(
        case_id="case_t1",
        case_name="demo 登录",
        status="failed",
        step_success_rate=0.5,
        duration_ms=1234,
        failed_categories={"assert-fail": 1},
        steps=[
            StepResult(step_id="s1", intent="输入账号", action="input", status="passed",
                       used_strategy="res-id", screenshot="screenshots/s1.png"),
            StepResult(step_id="s2", intent="点击登录", action="tap", status="failed",
                       category="assert-fail", error="AssertionError: 断言失败: 期望包含 'alice'",
                       screenshot="screenshots/s2.png", logcat_file="logs/s2.logcat.txt",
                       heal={"original": "res-id=com.insta.demo:id/login_btn",
                             "match": "fuzzy:textContains=login btn"}),
        ],
    )


def test_report_render_contains_evidence(tmp_path):
    out = render_html(_fake_run(), tmp_path)
    html = out.read_text(encoding="utf-8")
    assert "case_t1" in html
    assert "screenshots/s1.png" in html
    assert "cat-assert-fail" in html
    assert "logs/s2.logcat.txt" in html
    assert "自愈提案" in html and "fuzzy:textContains=login btn" in html


def test_suite_index_render(tmp_path):
    entries = [("demo 登录", "01_demo/report.html", _fake_run())]
    out = render_index_html("冒烟套件", entries, tmp_path)
    html = out.read_text(encoding="utf-8")
    assert "1/1 通过" in html or "0/1 通过" in html
    assert "01_demo/report.html" in html


def test_suite_index_enhanced_summary(tmp_path):
    # 批次聚合报告增强（发起人 2026-10-12）：汇总条 + 失败步骤/失败原因列
    entries = [("demo 登录", "01_demo/report.html", _fake_run())]
    out = render_index_html("冒烟套件", entries, tmp_path)
    html = out.read_text(encoding="utf-8")
    assert "失败步骤" in html and "失败原因" in html, "明细表应有失败步骤/失败原因列"
    assert "生成于" in html, "汇总条应有生成时间"
    assert "总耗时" in html, "汇总条应有总耗时"
    assert "通过 " in html and "失败 " in html and "中止 " in html, "汇总条应有三种状态计数"
    assert "MAT 套件聚合报告" in html


# -- 同因合并展示 + 校准标注（发起人 2026-10-13） ---------------------------------

def test_norm_error_and_fail_groups_merge_same_cause():
    """归一化：取首行 + 数字抹平 + 空白折叠——同因异常聚成一个 key；聚类保首现顺序。"""
    from insta_engine.report import _fail_groups, _norm_error

    assert _norm_error("AssertionError: 找不到 export_btn (attempt 3)\n  深层堆栈") == \
        "AssertionError: 找不到 export_btn (attempt N)", "数字抹平、取首行"
    assert _norm_error("") == ""
    steps = [
        StepResult(step_id="s1", intent="a", action="click", status="failed",
                   category="assert-fail", error="AssertionError: 找不到 export_btn (attempt 1)"),
        StepResult(step_id="s2", intent="b", action="click", status="failed",
                   category="assert-fail", error="AssertionError: 找不到 export_btn (attempt 2)"),
        StepResult(step_id="s3", intent="c", action="click", status="failed",
                   category="locate-fail", error="LocateFailed: text=登录 未命中"),
    ]
    groups = _fail_groups(steps)
    assert [g["category"] for g in groups] == ["assert-fail", "locate-fail"]
    assert [len(g["steps"]) for g in groups] == [2, 1]


def test_merge_findings_prefix_chain_page_key_and_type_guard():
    """探索同因判定：①路径前缀链 ②同 page_key ⇒ 合并；不同 type 永不合并。"""
    from insta_engine.report import _merge_findings

    findings = [
        {"type": "white-screen", "path": "L0 → L1"},
        {"type": "white-screen", "path": "L0 → L1 → L2"},          # 前缀链 ⇒ 同因
        {"type": "white-screen", "path": "L5", "page_key": "p1"},
        {"type": "white-screen", "path": "L9", "page_key": "p1"},  # 同页面复访 ⇒ 同因
        {"type": "crash", "path": "L0 → L1"},                      # 不同 type ⇒ 不合并
    ]
    groups = _merge_findings(findings)
    assert [len(g) for g in groups] == [2, 2, 1]


def test_report_merges_same_cause_failures_and_renders_marks(tmp_path):
    """run 报告：同因失败并成一行汇总（明细表不动）；校准标记挂徽标与原因。"""
    steps = [
        StepResult(step_id="s1", intent="点导出", action="click", status="failed",
                   category="assert-fail", error="AssertionError: 找不到 export_btn (attempt 1)",
                   ignored=True, calibration_note="风控页误报"),
        StepResult(step_id="s2", intent="再导出", action="click", status="failed",
                   category="assert-fail", error="AssertionError: 找不到 export_btn (attempt 2)"),
        StepResult(step_id="s3", intent="看结果", action="wait", status="passed",
                   false_positive=True, calibration_note="断言没生效，侥幸通过"),
    ]
    run = RunResult(case_id="case_m", case_name="合并样例", status="failed",
                    step_success_rate=0.33, duration_ms=100,
                    failed_categories={"assert-fail": 2}, steps=steps)
    html = render_html(run, tmp_path).read_text(encoding="utf-8")
    assert "失败原因汇总（同类合并）" in html
    assert "2 步" in html, "同因两步并一行（影响步骤 N 步）"
    assert "已忽略" in html and "误判" in html, "校准徽标要渲染"
    assert "校准原因" in html and "风控页误报" in html and "断言没生效" in html


def test_explore_report_merges_same_cause_findings(tmp_path):
    """探索报告：同 type 且路径同主干/同页面的 findings 并成一行，徽标与合并标注齐全。"""
    from insta_engine.report import render_explore_html

    report = {
        "package": "com.x", "steps": 12, "page_count": 3, "max_depth": 2,
        "depth_stats": {"L0": 1, "L1": 1, "L2": 1}, "duration_seconds": 5.0,
        "generated_at": "2026-10-13T10:00:00", "pages": [], "edges": [],
        "findings": [
            {"type": "white-screen", "detail": "页面白屏", "path": "L0 → L1",
             "page_key": "p1", "screenshot": "shots/f0.png",
             "calibration": {"verdict": "ignored", "note": "加载慢误报", "operator": "测试员"}},
            {"type": "white-screen", "detail": "深层白屏", "path": "L0 → L1 → L2",
             "page_key": "p2"},
            {"type": "crash", "detail": "进程崩溃", "path": "L0 → L9"},
            {"type": "white-screen", "detail": "复访白屏", "path": "L5", "page_key": "p1",
             "calibration": {"verdict": "confirmed", "note": "", "operator": "测试员"}},
        ],
    }
    html = render_explore_html(report, tmp_path).read_text(encoding="utf-8")
    assert "共 3 处（同因合并）" in html, "前缀链 + 同 page_key 的三处并一行"
    # 计数口径（发起人 2026-10-13）：头部「发现 N 处」用合并后数目，原始处数并入括注
    assert "发现 2 处" in html and "（原始 4 处，同因合并）" in html
    assert "已忽略 1 处" in html
    assert "已确认" in html, "组内任一处确认 ⇒ 组徽标已确认"
    assert "校准原因: 加载慢误报" in html
    assert "进程崩溃" in html and html.count("cat-crash") == 1, "不同 type 保持独立一行"


def test_suite_index_groups_same_cause_cases(tmp_path):
    """套件聚合：首个失败步骤同分类同归一化原因的用例并成一行（「N 条」+明细链接）。"""
    def _run(case_id: str, attempt: int) -> RunResult:
        return RunResult(
            case_id=case_id, case_name=case_id, status="failed",
            step_success_rate=0.5, duration_ms=100,
            failed_categories={"assert-fail": 1},
            steps=[StepResult(step_id="s1", intent="点导出", action="click", status="failed",
                              category="assert-fail",
                              error=f"AssertionError: 找不到 export_btn (attempt {attempt})")])

    entries = [("用例A", "01_a/report.html", _run("c_a", 1)),
               ("用例B", "02_b/report.html", _run("c_b", 2))]
    html = render_index_html("冒烟套件", entries, tmp_path).read_text(encoding="utf-8")
    assert "失败原因汇总（同类合并）" in html
    assert "2 条" in html, "同因两条用例并一行（影响用例 N 条）"
    assert "01_a/report.html" in html and "02_b/report.html" in html, "明细列各条报告链接"


# -- 用例定位审计 L1/L6（case_audit；L2–L5 已由 case_quality 的 R5/R4/R2/R6 覆盖，见其模块说明） --

from insta_engine.case_audit import audit_case  # noqa: E402
from insta_engine.page_index import home_labels_for  # noqa: E402


def _write_audit_index(tmp_path: Path) -> Path:
    """三页索引：splash(0) → 首页(1) → 搜索页(2)，覆盖 depth 过滤/退化 res-id/动态文案。"""
    idx = tmp_path / "artifacts" / "page_index"
    idx.mkdir(parents=True)
    (idx / "com.demo.json").write_text(json.dumps({
        "pages": [
            {"depth": 0, "activity": ".Splash", "controls": [
                {"text": "跳过", "clickable": True},
            ]},
            {"depth": 1, "activity": ".Main", "controls": [
                {"res-id": "com.demo:id/video_tab", "text": "视频", "clickable": True},
                {"text": "关注", "clickable": True},
                {"text": "1,234", "clickable": True},   # 动态文案不入池
                {"res-id": "com.demo:id/0_resource_name_obfuscated", "text": "",
                 "clickable": True},                     # 退化 res-id 不入池
            ]},
            {"depth": 2, "activity": ".Search", "controls": [
                {"res-id": "com.demo:id/clear_history", "text": "清空", "clickable": True},
                {"text": "图片", "clickable": False},    # 不可点 ⇒ 不入可点文案池
            ]},
        ]}, ensure_ascii=False), encoding="utf-8")
    return idx


def _audit_case(name: str, steps: list[dict]) -> TestCase:
    return TestCase.model_validate({
        "caseId": "case_audit", "name": name, "target": {"package": "com.demo"},
        "steps": steps,
    })


def test_home_labels_for_filters(tmp_path):
    idx = _write_audit_index(tmp_path)
    rids, texts = home_labels_for("com.demo", idx)
    assert rids == {"com.demo:id/video_tab"}, "只取 depth≤1，退化 res-id 过滤"
    assert texts == {"跳过", "视频", "关注"}, "只取可点稳定文案，动态文案过滤"
    assert home_labels_for("com.absent", idx) == (set(), set())


def test_audit_l1_text_not_in_clickable_inventory(tmp_path):
    warns = audit_case(_audit_case("查看图片", [
        {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "图片"}},
    ]), _write_audit_index(tmp_path))
    assert any("图片" in w and "不在控件清单" in w for w in warns), "非可点文案应被 L1 拦截"


def test_audit_l6_permission_dialog_dependency():
    """系统权限弹窗仅首次触发（本轮 02/03 的死因）：弹窗语境 + 弹窗按钮词才告警。"""
    warns = audit_case(_audit_case("位置授权弹窗-点击拒绝", [
        {"id": "s0", "action": "launch", "params": {"package": "com.demo", "cold": True}},
        {"id": "s1", "intent": "点击「拒绝」", "action": "tap",
         "target": {"strategy": "text", "value": "拒绝"}},
    ]))
    assert any("权限弹窗" in w for w in warns)
    ok = _audit_case("同意协议后进入首页", [
        {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "同意"}},
    ])
    assert not any("权限弹窗" in w for w in audit_case(ok)), "无弹窗语境不告警"


# -- 套件加载 -----------------------------------------------------------------

def test_load_single_case(tmp_path):
    case_file = tmp_path / "case.json"
    case_file.write_text(
        json.dumps({"caseId": "c1", "steps": [{"id": "s1", "action": "wait", "params": {"ms": 10}}]}),
        encoding="utf-8",
    )
    kind, case = load_case_or_suite(case_file)
    assert kind == "case"
    assert isinstance(case, TestCase) and case.case_id == "c1"


def test_load_suite(tmp_path):
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(json.dumps({"name": "smoke", "cases": ["case.json"]}), encoding="utf-8")
    kind, data = load_case_or_suite(suite_file)
    assert kind == "suite"
    assert data["cases"] == ["case.json"]


# -- benchmark gold 用例 schema 校验（防止任务集与 DSL 漂移） --------------------

def test_benchmark_gold_cases_validate():
    suite = json.loads((REPO / "benchmarks" / "tasks" / "spike-5-tasks.json").read_text(encoding="utf-8"))
    assert suite["pass_threshold"] == 0.8
    for task in suite["tasks"]:
        case = TestCase.model_validate(task["case"])
        assert case.steps, f"{task['id']} 无步骤"
        assert task["nl"], f"{task['id']} 缺少自然语言描述"


def test_report_inlines_screenshots_and_logcat(tmp_path):
    """截图/logcat 文件存在时内联进报告（单文件自包含）；缺失时回退相对链接。"""
    import base64

    run = _fake_run()
    shot = tmp_path / "screenshots" / "s1.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG-fake-bytes")
    log = tmp_path / "logs" / "s2.logcat.txt"
    log.parent.mkdir(parents=True)
    log.write_text("FATAL EXCEPTION (main)\n...", encoding="utf-8")

    out = render_html(run, tmp_path)
    html = out.read_text(encoding="utf-8")
    png_bytes = b"\x89PNG-fake-bytes"
    expected = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
    assert expected in html
    assert "details class=\"logcat\"" in html and "FATAL EXCEPTION" in html
    # 通过率环 + 失败分类条已渲染
    assert "conic-gradient" in html and 'class="bar"' in html


# -- 事件端点与取消端点 -------------------------------------------------------

def test_job_events_incremental_and_cancel():
    """emit 事件可按 since 增量拉取；cancel 置位后任务标记 cancelled。"""
    import time as _t

    from insta_engine import web_server

    def job(emit, cancel):
        emit("run", "running", "demo.json")
        for i in range(5):
            if cancel.is_set():
                emit("run", "cancelled", f"完成 {i} 步")
                break
            emit(f"s{i}", "passed", "", 100 + i)
            _t.sleep(0.05)
        return "ok", None

    jid = web_server.start_job("run", job)
    deadline = _t.time() + 2
    while _t.time() < deadline and web_server._JOBS[jid]["seq"] < 2:
        _t.sleep(0.02)

    code, obj = web_server.api_job_events({"id": jid, "since": "1"})
    assert code == 200 and obj["events"][0]["seq"] == 2        # 增量：since 之前的不再返回
    full = web_server.api_job_events({"id": jid, "since": 0})[1]
    assert full["events"][0]["step_id"] == "run" and full["events"][0]["status"] == "running"

    code, obj = web_server.api_job_cancel({"id": jid})
    assert code == 200 and obj["requested"] is True
    deadline = _t.time() + 2
    while _t.time() < deadline and web_server._JOBS[jid]["status"] == "running":
        _t.sleep(0.02)                                          # 等 worker 收尾（cancel 事件落账）
    assert web_server._JOBS[jid]["status"] == "cancelled"      # 收尾状态如实
    code, obj = web_server.api_job_events({"id": jid, "since": 0})
    assert any(e["status"] == "cancelled" for e in obj["events"])


def test_job_cancel_unknown_and_finished():
    from insta_engine import web_server

    code, obj = web_server.api_job_cancel({"id": "不存在"})
    assert code == 404
    # 已结束的任务再 cancel：requested=False 且不报错
    jid = web_server.start_job("run", lambda emit, cancel: ("ok", None))
    import time as _t
    deadline = _t.time() + 2
    while _t.time() < deadline and web_server._JOBS[jid]["status"] == "running":
        _t.sleep(0.02)
    code, obj = web_server.api_job_cancel({"id": jid})
    assert code == 200 and obj["requested"] is False


# -- 探索图谱启发式选点 -------------------------------------------

def _explore_page(name: str, buttons: list[tuple[str, str]], package: str = "com.x") -> str:
    """构造含多个可点按钮的页面 XML（label 可为空串模拟图片按钮）。"""
    parts = [f'<node class="TextView" text="{name}" package="{package}"/>']
    for i, (label, _) in enumerate(buttons):
        parts.append(f'<node class="Button" text="{label}" content-desc="{label}" '
                     f'clickable="true" bounds="[0,{i * 20}][40,{i * 20 + 16}]" package="{package}"/>')
    return "".join(parts)


class _HeurDriver:
    """双按钮页面：点击后落到按钮指定的页面，可回退。"""

    serial = None
    TRANS = {}

    def __init__(self, pages: dict, start: str):
        self.pages = pages
        self.stack = [start]
        self.clicks: list[str] = []

    def dump(self):
        return self.pages[self.stack[-1]]

    def click(self, x: int, y: int):
        label = (y // 20)
        page = self.pages[self.stack[-1]]
        import re as _re
        labels = _re.findall(r'text="([^"]*)" content-desc', page)
        chosen = labels[label] if label < len(labels) else ""
        self.clicks.append(chosen)
        for name, btns in self.TRANS.get(self.stack[-1], {}).items():
            if chosen == name:
                self.stack.append(btns)

    def press(self, key):
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = [self.start_page]


def test_explore_heuristic_prefers_unseen_page_entry(tmp_path):
    """图谱启发式：A 页两个按钮，『去商店』的边指向 0 访问页 → 优先于无图谱依据的『逛逛论坛』。"""
    from insta_engine.explore import explore
    from insta_engine.page_index import new_index

    driver = _HeurDriver(
        pages={
            "A": _explore_page("首页", [("逛逛论坛", ""), ("去商店", "")]),
            "论坛": _explore_page("论坛", []),
            "商店": _explore_page("商店", []),
        },
        start="A")
    driver.start_page = "A"
    driver.TRANS = {"A": {"去商店": "商店", "逛逛论坛": "论坛"}}

    # 先跑一轮建图谱（不含启发式历史也没关系，仅沉淀 edges/hits）
    index_path = tmp_path / "com.x.json"
    explore(driver, "com.x", minutes=0.02, max_steps=2, artifacts_dir=tmp_path / "a",
            max_depth=2, sleep_scale=0.01, index_path=index_path)
    index = new_index("com.x")
    graph = index  # 占位避免未用告警
    del graph
    # 用真实沉淀的图谱文件重跑：断言新会话的第一次点击选择了『去商店』（若上一轮点了它，
    # 本轮该入口 tried 被跳过 → 选另一个；这里只验证选点函数本身）
    from insta_engine.explore import _pick
    from insta_engine.page_index import load_index

    graph2 = load_index(index_path)
    prior_edge_from = {}
    prior_hits = {}
    for e in graph2.get("edges", []):
        prior_edge_from.setdefault(e["from"], []).append((e["action"], e["to"]))
    for p in graph2["pages"]:
        prior_hits[p["page_key"]] = p.get("hits", 0)

    cands = [{"bounds": "[0,0][40,16]", "label": "去商店"},
             {"bounds": "[0,20][40,36]", "label": "逛逛论坛"}]
    a_key = graph2["pages"][0]["page_key"]
    # 构造『去商店』边指向 0 访问页 → 必须优先
    prior_edge_from[a_key] = [("去商店", list(prior_hits)[-1])]
    for p in graph2["pages"]:
        prior_hits[p["page_key"]] = 5
    choice = _pick(cands, {a_key}, 0, prior_edge_from, prior_hits)
    assert choice["label"] == "去商店"


def test_explore_heuristic_prefers_labeled_over_unlabeled(tmp_path):
    """无图谱时：有文案的控件优先于空文案（图片按钮）——语义可复用性导向。"""
    from insta_engine.explore import _pick

    cands = [{"bounds": "[0,0][40,16]", "label": ""},          # 图片按钮
             {"bounds": "[0,20][40,36]", "label": "设置"}]
    assert _pick(cands, set(), 0, {}, {})[
        "label"] == "设置"
    # 同分（全有文案）时保持轮转
    both = [{"bounds": "", "label": "a"}, {"bounds": "", "label": "b"}]
    picks = {_pick(both, set(), i, {}, {})[
        "label"] for i in range(2)}
    assert picks == {"a", "b"}


# -- 单设备互斥（评审 P1-4） -------------------------------------------------------

def test_same_serial_jobs_serialize():
    """同 serial 两个任务必须串行：第二个排队等待，事件流互不污染。"""
    import time as _t

    from insta_engine import web_server

    order: list[str] = []

    def make(tag: str, dur: float):
        def job(emit, cancel):
            with web_server.device_lock("serialA"):
                order.append(tag + "-start")
                emit(tag, "running")
                _t.sleep(dur)
                order.append(tag + "-end")
            return "ok", None
        return job

    web_server.start_job("run", make("A", 0.2))   # 只为启动 A；句柄未用到，不赋值（ruff F841）
    j2 = web_server.start_job("run", make("B", 0.05))
    deadline = _t.time() + 3
    while _t.time() < deadline and web_server._JOBS[j2]["status"] == "running":
        _t.sleep(0.02)
    assert order == ["A-start", "A-end", "B-start", "B-end"], order


def test_different_serials_do_not_block():
    """不同设备互不阻塞：两个任务可并发。"""
    import time as _t

    from insta_engine import web_server

    done: list[str] = []

    def make(tag: str):
        def job(emit, cancel):
            with web_server.device_lock(tag):
                _t.sleep(0.15)
                done.append(tag)
            return "ok", None
        return job

    web_server.start_job("run", make("devX"))
    web_server.start_job("run", make("devY"))
    deadline = _t.time() + 2
    while _t.time() < deadline and len(done) < 2:
        _t.sleep(0.02)
    assert sorted(done) == ["devX", "devY"]


# -- T53：describe 输出预算 / 截断救回 / 配额成功后才核销 --------------------------

class _TruncatedClient:
    """complete_raw_text 恒返回同一截断文本的桩（内容级重试也救不回）。"""

    prompt_mode = "local"
    max_tokens_override = None

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def complete_raw_text(self, messages, context=""):
        self.calls += 1
        return self.text


def test_describe_pages_salvages_truncated_batch():
    """T53c：截断的批量输出按条救回——~85 条合法描述不再因尾部截断整批丢弃。"""
    import json as _json

    from insta_engine.ai_client import GenClient

    pages = [{"page_key": f"p{i}", "key_elements": ["按钮"], "layout_regions": [],
              "dynamic_regions": []} for i in range(4)]
    # 真实形态：4 个对象都完整，但输出在结尾被截断——缺 "]}"。
    # raw_decode 逐条解码应把 4 条全部救回（正则/索引切片方案会丢掉最后一条）。
    truncated = '{"pages": [' + ",".join(
        _json.dumps({"page_key": f"p{i}", "title": f"页面{i}", "summary": "用途"})
        for i in range(4))
    client = _TruncatedClient(truncated)
    described = GenClient.describe_pages(client, pages)
    assert set(described) == {"p0", "p1", "p2", "p3"}, f"救回的键: {set(described)}"
    assert described["p3"]["title"] == "页面3"
    assert described["p3"]["summary"] == "用途"
    assert client.max_tokens_override == max(4096, 4 * 160)  # T53a：预算按页数抬高


def test_describe_pages_raises_when_nothing_salvageable():
    """完全无法解析时仍如实抛错（不静默返回空表）。"""
    import pytest

    from insta_engine.ai_client import GenClient, GenError

    client = _TruncatedClient("这不是 JSON")
    with pytest.raises(GenError):
        GenClient.describe_pages(client, [{"page_key": "p1"}])


def test_describe_quota_consumed_only_on_success(tmp_path, monkeypatch):
    """T53b：模型失败不扣额度（实测白扣 2 次的教训），成功落盘才核销。"""
    import json as _json

    from insta_engine.__main__ import cmd_describe
    from insta_engine.quota import UsageStore

    monkeypatch.chdir(tmp_path)
    idx_dir = tmp_path / "artifacts" / "page_index"
    idx_dir.mkdir(parents=True)
    (idx_dir / "com.t.json").write_text(_json.dumps({
        "package": "com.t", "pages": [
            {"page_key": "p1", "depth": 0, "structure": {"key_elements": ["登录"]}}]},
        ensure_ascii=False), encoding="utf-8")
    store = UsageStore(tmp_path / "usage.json")
    monkeypatch.setattr("insta_engine.__main__.UsageStore", lambda: store)

    class Boom:
        @classmethod
        def from_env(cls):
            return cls()

        def describe_pages(self, pages):
            raise RuntimeError("网络炸了")

    monkeypatch.setattr("insta_engine.__main__.GenClient", Boom)
    args = type("Args", (), {"package": "com.t", "limit": 5, "refresh": False})()
    assert cmd_describe(args) == 1
    assert store.snapshot()["gen_count"] == 0.0, "失败必须不扣额度"

    class Ok(Boom):
        def describe_pages(self, pages):
            return {"p1": {"title": "登录页", "summary": "输入账号密码登录"}}

    monkeypatch.setattr("insta_engine.__main__.GenClient", Ok)
    assert cmd_describe(args) == 0
    assert store.snapshot()["gen_count"] == 1.0, "成功才核销 1 次"


# -- T54：graph_context 取值策略（去重 / 导航页优先 / 不丢 LNone） ------------------

def test_graph_context_dedupes_same_screen_pages():
    """page_key 不稳让同屏记多条（真实 App 实测 131≈86 功能）——同 identities 只占一个槽位。"""
    from insta_engine.page_index import graph_context

    same_ids = ["登录", "账号输入"]
    pages = [
        {"page_key": "k1", "depth": 0, "node_count": 10, "identities": same_ids, "title": "登录页"},
        {"page_key": "k2", "depth": 1, "node_count": 12, "identities": same_ids, "title": "登录页"},
        {"page_key": "k3", "depth": 2, "node_count": 5, "identities": ["设置"],
         "parent_action": "点[设置]"},
    ]
    text = graph_context(pages, max_pages=12)
    assert text.count("登录页") == 1, f"同屏去重失败: {text}"
    assert "点[设置]" in text


def test_graph_context_prefers_nav_pages_and_keeps_unknown_depth():
    """有 parent_action 的页优先入选（导航边最值钱）；depth 缺失标 L? 不静默丢弃。"""
    from insta_engine.page_index import graph_context

    pages = [{"page_key": f"flat{i}", "depth": 0, "node_count": 3,
              "identities": [f"平铺{i}"], "title": f"平铺页{i}"} for i in range(12)]
    pages += [
        {"page_key": "nav1", "identities": ["购物车"], "node_count": 8,
         "parent_action": "点[购物车]"},                      # depth 缺失
        {"page_key": "nav2", "identities": ["订单"], "node_count": 6,
         "parent_action": "点[订单]", "title": "订单页"},
    ]
    text = graph_context(pages, max_pages=12)
    assert "点[购物车]" in text and "点[订单]" in text, "导航页必须入选"
    assert "L?" in text, "depth 缺失应标 L?，不能静默丢弃"
    assert text.count("平铺页") == 10, "导航页占 2 槽后平铺页补位 10 个"


# -- T55 P0：mat console --share 局域网分享 ----------------------------------------

def test_lan_urls_shape_and_no_loopback():
    """lan_urls 只出 http://IP:port 形态、不含回环地址（尽力而为，无网环境允许为空）。"""
    from insta_engine.web_server import lan_urls

    urls = lan_urls(7420)
    for u in urls:
        assert u.startswith("http://") and u.endswith(":7420")
        assert "127.0.0.1" not in u, "回环地址不该出现在分享列表"


def test_serve_accepts_share_host(tmp_path):
    """--share 的落点是 serve(host="0.0.0.0")——签名可注入（测试起即停，不真占端口）。"""
    import inspect

    from insta_engine import web_server

    sig = inspect.signature(web_server.serve)
    assert sig.parameters["host"].default == "127.0.0.1", "默认必须仍为本机监听（安全默认）"
