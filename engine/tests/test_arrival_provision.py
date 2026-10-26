"""执行期前置置备测试：状态不符 → 图谱父链自动到达（「求解器」最小实现）。"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine.dsl import TestCase
from insta_engine.page_index import load_index, save_index

# -- 假驱动：A --去B--> B --去C--> C 状态机（与 test_arrivals.FakeExploreDriver 同构）------


class FakeNavDriver:
    serial = None
    XML = {
        "A": '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "B": '<node class="Button" text="去C" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "C": '<node class="TextView" text="C页面" package="com.x"/>',
    }
    TRANS = {("A", 5): "B", ("B", 5): "C"}

    def __init__(self, start: str = "A", navigate: bool = True):
        self.stack = [start]
        self.navigate = navigate
        self.starts = 0
        self.clicks: list[tuple[str, int, int]] = []

    def dump(self):
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        self.clicks.append((self.stack[-1], x, y))
        if self.navigate:
            target = self.TRANS.get((self.stack[-1], x))
            if target:
                self.stack.append(target)

    def app_start(self, package, activity=None):
        self.starts += 1
        self.stack = ["A"]

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")


def _build_graph(tmp_path: Path) -> Path:
    """跑一轮 explore 沉淀 A/B/C 三层图谱（与真机流程一致）。"""
    from insta_engine.explore import explore

    index_path = tmp_path / "com.x.json"
    explore(FakeNavDriver(), "com.x", minutes=0.05, max_steps=6,
            artifacts_dir=tmp_path / "art", max_depth=3, sleep_scale=0.01,
            index_path=index_path)
    return index_path


def _case(target: tuple[str, str] | None = ("text", "去C")) -> TestCase:
    """launch + 1 个动作步骤（A8 典型形状：冷启动直接点深层控件）。"""
    steps: list[dict] = [{"id": "s0", "action": "launch"}]
    if target is not None:
        steps.append({"id": "s1", "action": "tap",
                      "target": {"strategy": target[0], "value": target[1]}})
    return TestCase.model_validate(
        {"caseId": "c", "name": "n", "target": {"package": "com.x"}, "steps": steps})


# -- 推断 --------------------------------------------------------------------------------------


def test_infer_unique_page(tmp_path):
    """首个动作步骤的定位值在图谱里**唯一**命中 ⇒ 返回该页（B 拥有「去C」控件）。"""
    from insta_engine.arrival_provision import infer_target_page

    index = load_index(_build_graph(tmp_path))
    pages = index["pages"]
    page = infer_target_page(_case(), pages)
    assert page is not None
    assert page["depth"] == 1  # B 是一跳深页
    assert any(c.get("text") == "去C" for c in page["controls"])


def test_infer_ambiguous_returns_none():
    """定位值跨 **activity** 命中（真歧义）⇒ None——宁可不动，不可走错。

    ⚠️ 同 activity 内的多个 page_key 命中**不算歧义**（A/B 实测：同页稳定性仅 12%，
    同一逻辑页常碎片化成多个 key）——那种情况返回代表页（见下一条测试）。
    """
    from insta_engine.arrival_provision import infer_target_page

    pages = [
        {"page_key": "p1", "activity": "com.x/.A", "controls": [{"text": "重合"}]},
        {"page_key": "p2", "activity": "com.x/.B", "controls": [{"text": "重合"}]},
    ]
    assert infer_target_page(_case(("text", "重合")), pages) is None


def test_infer_fragmented_pages_pick_representative():
    """同 activity 多碎片命中 ⇒ 选代表页：arrival stable 优先，其次 hits 最多。"""
    from insta_engine.arrival_provision import infer_target_page

    pages = [
        {"page_key": "p_frag1", "activity": "com.x/.A", "hits": 3,
         "controls": [{"text": "去C"}]},
        {"page_key": "p_frag2", "activity": "com.x/.A", "hits": 9,
         "controls": [{"text": "去C"}]},
        {"page_key": "p_frag3", "activity": "com.x/.A", "hits": 1,
         "arrival": {"status": "stable"}, "controls": [{"text": "去C"}]},
    ]
    page = infer_target_page(_case(("text", "去C")), pages)
    assert page is not None and page["page_key"] == "p_frag3"  # stable 优先于 hits


def test_infer_no_match_or_no_target_returns_none():
    from insta_engine.arrival_provision import infer_target_page

    pages = [{"page_key": "p1", "controls": [{"text": "别的", "clickable": True}]}]
    assert infer_target_page(_case(("text", "不存在")), pages) is None
    assert infer_target_page(_case(target=None), pages) is None  # 只有 launch


def test_infer_by_res_id():
    from insta_engine.arrival_provision import infer_target_page

    pages = [{"page_key": "p1", "controls": [{"res-id": "com.x:id/btn"}]}]
    assert infer_target_page(_case(("res-id", "com.x:id/btn")), pages)["page_key"] == "p1"


# -- 置备 --------------------------------------------------------------------------------------


def test_provision_navigates_to_target_page(tmp_path):
    """设备在 A、用例要点 B 页的控件 ⇒ 自动走到 B，证据 ok=True。"""
    from insta_engine.arrival_provision import provision

    drv = FakeNavDriver(start="A")
    ev = provision(_case(), drv, "com.x", _build_graph(tmp_path), sleep_scale=0.01)
    assert ev["needed"] is True and ev["ok"] is True, ev
    assert ev["hops"] == 1 and ev["clicks"] == 1
    assert drv.stack[-1] == "B"  # 设备真的被带到了目标页
    assert ev["entry"] and ev["entry"][-1] == {"action": "tap", "target": "去B"}


def test_provision_skips_when_already_on_target(tmp_path):
    from insta_engine.arrival_provision import provision

    drv = FakeNavDriver(start="B")
    ev = provision(_case(), drv, "com.x", _build_graph(tmp_path), sleep_scale=0.01)
    assert ev["needed"] is False and "已在目标页" in ev["reason"], ev
    assert drv.clicks == []  # 什么都没点


def test_provision_infer_failure_is_noop(tmp_path):
    """推断不出目标页（用例只有 launch / 定位无命中）⇒ needed=False 原行为照旧。"""
    from insta_engine.arrival_provision import provision

    drv = FakeNavDriver(start="A")
    ev = provision(_case(target=None), drv, "com.x", _build_graph(tmp_path))
    assert ev["needed"] is False and drv.clicks == []


def test_provision_broken_chain_reports_not_ok(tmp_path):
    """父链缺环 ⇒ needed=True 但 ok=False（明确报因，不瞎走）。"""
    from insta_engine.arrival_provision import provision

    index_path = _build_graph(tmp_path)
    index = load_index(index_path)
    for p in index["pages"]:
        if p["depth"] == 1:  # 把 B 的 parent 掐断
            p["parent_key"] = "ghost"
    save_index(index_path, index)

    drv = FakeNavDriver(start="A")
    ev = provision(_case(), drv, "com.x", index_path, sleep_scale=0.01)
    assert ev["needed"] is True and ev["ok"] is False, ev
    assert "父链不完整" in ev["reason"] and drv.clicks == []


def test_provision_landing_mismatch_reports_not_ok(tmp_path):
    """点了但没到页（弹窗吞了点击）⇒ ok=False 且原因含「落点不符」。"""
    from insta_engine.arrival_provision import provision

    drv = FakeNavDriver(start="A", navigate=False)  # 点击不换页
    ev = provision(_case(), drv, "com.x", _build_graph(tmp_path), sleep_scale=0.01)
    assert ev["needed"] is True and ev["ok"] is False, ev
    assert "落点不符" in ev["reason"]


def test_provision_never_raises_on_driver_errors(tmp_path):
    """dump 挂掉等驱动异常 ⇒ 降级返回，绝不抛出。"""
    from insta_engine.arrival_provision import provision

    class DeadDriver(FakeNavDriver):
        def dump(self):
            raise RuntimeError("usb 掉了")

    ev = provision(_case(), DeadDriver(), "com.x", _build_graph(tmp_path))
    assert ev["needed"] is False and "识别失败" in ev["reason"], ev


# -- 判据同步（2026-10-17：与 arrivals 验证侧同口径）---------------------------------------------


def _write_index(tmp_path: Path, pages: list[dict], **extra) -> Path:
    from insta_engine.page_index import merge_pages, new_index

    index = new_index("com.x")
    merge_pages(index, pages)
    index.update(extra)
    index_path = tmp_path / "com.x.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return index_path


def test_provision_back_hop_chain(tmp_path):
    """BACK_HOP 链（先前进到深层页、再按 back 回目标页）⇒ 置备走通，证据如实标 back。"""
    from insta_engine.arrival_provision import provision
    from insta_engine.page_index import BACK_HOP, page_key

    class BackChainDriver(FakeNavDriver):
        XML = {
            "A": '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
            "B": '<node class="TextView" text="B页面弹窗" package="com.x"/>',
            "C": '<node class="TextView" text="C页面目标" package="com.x"/>',
        }
        TRANS = {("A", 5): "B"}

        def press(self, key):
            # 模拟 explore 的「回退恢复」：B 是弹窗页，按 back 关掉它露出未收录的底层 C
            if key == "back" and self.stack[-1] == "B":
                self.stack.append("C")

    drv = BackChainDriver(start="A")
    ka = page_key(BackChainDriver.XML["A"], "com.x")
    kb = page_key(BackChainDriver.XML["B"], "com.x")
    kc = page_key(BackChainDriver.XML["C"], "com.x")
    index_path = _write_index(tmp_path, [
        {"page_key": ka, "depth": 0, "controls": [{"text": "去B", "clickable": True}]},
        {"page_key": kb, "depth": 1, "parent_key": ka, "parent_action": "去B",
         "controls": [{"text": "B页面弹窗"}]},
        {"page_key": kc, "depth": 2, "parent_key": kb, "parent_action": BACK_HOP,
         "controls": [{"text": "C页面目标"}]},
    ])

    ev = provision(_case(("text", "C页面目标")), drv, "com.x", index_path,
                   sleep_scale=0.01)
    assert ev["ok"] is True, ev
    assert drv.stack[-1] == "C"      # 前进到弹窗页 B 后按 back 真的露出了底层目标页
    assert ev["entry"][-1] == {"action": "back", "target": ""}   # back 跳如实标注、无点击目标
    assert ev["clicks"] == 2         # 1 次点击 + 1 次 back


def test_provision_transient_root_gives_up_before_clicking(tmp_path):
    """根为瞬态启动帧（含「跳过」）⇒ 与 verify_arrivals 同判据零成本放弃，不冷启动不点击。"""
    from insta_engine.arrival_provision import provision

    index_path = _build_graph(tmp_path)
    index = load_index(index_path)
    for p in index["pages"]:
        if p["depth"] == 0:   # 只改 controls，不动 page_key——预判只看控件
            p["controls"].append({"text": "跳过", "clickable": True})
    save_index(index_path, index)

    drv = FakeNavDriver(start="A")
    ev = provision(_case(), drv, "com.x", index_path, sleep_scale=0.01)
    assert ev["needed"] is True and ev["ok"] is False, ev
    assert "瞬态" in ev["reason"] and drv.clicks == [] and drv.starts == 0


def test_provision_unreachable_root_gives_up_before_clicking(tmp_path):
    """图谱记录 launch_activity 且根 activity 与之不符 ⇒ 零成本放弃（结构性不可重放）。"""
    from insta_engine.arrival_provision import provision

    index_path = _build_graph(tmp_path)
    index = load_index(index_path)
    index["launch_activity"] = "com.x/.Home"
    for p in index["pages"]:
        if p["depth"] == 0:
            p["activity"] = "com.x/.Legacy"   # 遗留页根（真机实测：播客详情页根）
    save_index(index_path, index)

    drv = FakeNavDriver(start="A")
    ev = provision(_case(), drv, "com.x", index_path, sleep_scale=0.01)
    assert ev["needed"] is True and ev["ok"] is False, ev
    assert "launch 不可达" in ev["reason"] and drv.clicks == [] and drv.starts == 0


class _DriftNavDriver(FakeNavDriver):
    """B 页带结构 rid，落点附加每轮不同的快照 text（内容轮换页，test_arrivals 同构）。"""

    XML = {**FakeNavDriver.XML,
           "B": ('<node class="Button" text="去C" resource-id="com.x:id/btn_c" '
                 'clickable="true" bounds="[0,0][10,10]" package="com.x"/>')}

    def __init__(self, start="A", navigate=True, activity="com.x/.MainActivity",
                 gen=0):
        super().__init__(start, navigate)
        self.activity = activity
        self.gen = gen

    def click(self, x, y):
        if self.stack[-1] == "A":
            self.gen += 1
        super().click(x, y)

    def dump(self):
        if self.stack[-1] == "B":
            return (self.XML["B"]
                    + f'<node class="TextView" text="快照{self.gen}" package="com.x"/>')
        return self.XML[self.stack[-1]]

    def foreground_app(self):
        return {"package": "com.x", "activity": self.activity}


def _build_drift_graph(tmp_path: Path) -> Path:
    """建图与置备同一 driver 形态：图谱 B 页才带 rid 信号与 activity（对齐基准）。"""
    from insta_engine.explore import explore

    index_path = tmp_path / "com.x.json"
    explore(_DriftNavDriver(), "com.x", minutes=0.05, max_steps=6,
            artifacts_dir=tmp_path / "art", max_depth=3, sleep_scale=0.01,
            index_path=index_path)
    return index_path


def test_provision_drift_landing_aligned_single_round(tmp_path):
    """内容轮换页：走完链但落点快照不在图谱 ⇒ 单轮结构对齐（activity 一致 + rid 对齐）放行。"""
    from insta_engine.arrival_provision import provision

    index_path = _build_drift_graph(tmp_path)
    drv = _DriftNavDriver(start="A", gen=5)   # 前台在本包（on_root）→ 不冷启动直接走链
    ev = provision(_case(), drv, "com.x", index_path, sleep_scale=0.01)
    assert ev["ok"] is True, ev
    assert "碎片" in ev["reason"] and "对齐" in ev["reason"], ev
    assert drv.stack[-1] == "B"   # 设备真的被带到了目标页的功能页上


def test_provision_drift_not_aligned_when_activity_differs(tmp_path):
    """落点 activity 与目标页不同（真跳到了别页）⇒ 单轮对齐不放行，维持诚实失败。

    根页 activity 清空让 _walk_chain 的根核对不表态（"不知道"）——才能走完链、
    落到终点对齐分支，测到「activity 约束」本身而非被根核对提前拦下。
    """
    from insta_engine.arrival_provision import provision

    index_path = _build_drift_graph(tmp_path)
    index = load_index(index_path)
    for p in index["pages"]:
        if p.get("depth") == 0:
            p["activity"] = ""            # 根核对不表态
    save_index(index_path, index)

    drv = _DriftNavDriver(start="A", gen=5, activity="com.x/.Other")
    ev = provision(_case(), drv, "com.x", index_path, sleep_scale=0.01)
    assert ev["ok"] is False and "落点不符" in ev["reason"], ev


# -- runner 钩子 --------------------------------------------------------------------------------


def test_runner_records_arrival_evidence(monkeypatch, tmp_path):
    """run() 在首个非 launch 步骤前置备一次，证据进 RunResult.arrival。"""
    from insta_engine import arrival_provision
    from insta_engine.runner import Runner

    calls = {"n": 0}
    expected = {"needed": True, "ok": True, "page": "abcdef12", "hops": 2,
                "clicks": 2, "reason": "", "entry": []}

    def fake_provision(case, drv, package, index_path, **kw):
        calls["n"] += 1
        calls["package"] = package
        return expected

    monkeypatch.setattr(arrival_provision, "provision", fake_provision)
    monkeypatch.delenv("MAT_AUTO_ARRIVE", raising=False)
    (tmp_path / "com.x.json").write_text("{}", encoding="utf-8")  # 仅过存在性检查

    case = TestCase.model_validate(
        {"caseId": "c", "name": "n", "target": {"package": "com.x"},
         "steps": [{"id": "s0", "action": "launch"},
                   {"id": "s1", "action": "wait"},
                   {"id": "s2", "action": "wait"}]})
    run = Runner(FakeNavDriver(), tmp_path, index_path=tmp_path / "com.x.json").run(case)
    assert run.arrival == expected  # 证据原样透传
    assert calls == {"n": 1, "package": "com.x"}  # 多个非 launch 步骤只置备一次


def test_runner_gate_and_launch_only(monkeypatch, tmp_path):
    """MAT_AUTO_ARRIVE=0 关闭；纯 launch 用例不触发置备。"""
    from insta_engine import arrival_provision
    from insta_engine.runner import Runner

    calls = {"n": 0}

    def fake_provision(case, drv, package, index_path, **kw):
        calls["n"] += 1
        return {"needed": False, "ok": None, "reason": ""}

    monkeypatch.setattr(arrival_provision, "provision", fake_provision)

    launch_only = TestCase.model_validate(
        {"caseId": "c", "name": "n", "target": {"package": "com.x"},
         "steps": [{"id": "s0", "action": "launch"}]})
    idx = tmp_path / "com.x.json"
    idx.write_text("{}", encoding="utf-8")
    run = Runner(FakeNavDriver(), tmp_path, index_path=idx).run(launch_only)
    assert run.arrival is None and calls["n"] == 0  # launch 不触发

    case = TestCase.model_validate(
        {"caseId": "c2", "name": "n", "target": {"package": "com.x"},
         "steps": [{"id": "s0", "action": "launch"}, {"id": "s1", "action": "wait"}]})
    monkeypatch.setenv("MAT_AUTO_ARRIVE", "0")
    run = Runner(FakeNavDriver(), tmp_path, index_path=idx).run(case)
    assert "已关闭" in (run.arrival or {}).get("reason", "") and calls["n"] == 0


def test_runner_no_graph_degrades(monkeypatch, tmp_path):
    """没有图谱 ⇒ needed=False 且不碰 provision（纯降级，不报错）。"""
    from insta_engine import arrival_provision
    from insta_engine.runner import Runner

    def boom(*a, **kw):
        raise AssertionError("无图谱不应调用 provision")

    monkeypatch.setattr(arrival_provision, "provision", boom)
    monkeypatch.delenv("MAT_AUTO_ARRIVE", raising=False)
    case = TestCase.model_validate(
        {"caseId": "c", "name": "n", "target": {"package": "com.x"},
         "steps": [{"id": "s0", "action": "launch"}, {"id": "s1", "action": "wait"}]})
    run = Runner(FakeNavDriver(), tmp_path, index_path=tmp_path / "absent.json").run(case)
    assert run.arrival["needed"] is False and "无页面图谱" in run.arrival["reason"]


def test_infer_target_page_skips_transient_splash_frame():
    """首步是「点击跳过」时，不能把开屏广告页（瞬态帧）当成期望起始页。

    实测背景（2026-10-21 tx-news 6 条）：用例第一步是「跳过」开屏广告，
    `infer_target_page` 据此把 splash 页当成目标页；该页是瞬态帧、没有父链
    ⇒ 置备报"父链不完整"直接放弃，用例从当前页继续，第一步就找不到「跳过」。
    正确行为：只命中瞬态帧 ⇒ 返回 None（宁可不动，不可走错）。
    """
    from insta_engine.arrival_provision import infer_target_page
    from insta_engine.dsl import (
        Action,
        Assertion,
        AssertionType,
        Locator,
        LocatorStrategy,
        Step,
        TestCase,
    )

    splash = {"page_key": "splash", "depth": None, "activity": "A/.Splash",
              "controls": [{"text": "跳过", "clickable": True}],
              "identities": ["text:跳过"], "hits": 1}
    home = {"page_key": "home", "depth": 0, "activity": "A/.Main",
            "controls": [{"text": "推荐", "clickable": True},
                         {"text": "要闻", "clickable": True}],
            "identities": ["text:推荐"], "hits": 5}

    case = TestCase(
        caseId="case_000001", name="开屏广告", target={"package": "com.x"},
        steps=[
            Step(id="s1", action=Action.LAUNCH),
            Step(id="s2", action=Action.TAP,
                 target=Locator(strategy=LocatorStrategy.TEXT, value="跳过"),
                 assertions=[Assertion(type=AssertionType.EXISTS, expected=None)]),
        ],
    )
    # 只有瞬态帧命中 ⇒ 推断不出（不置备）
    assert infer_target_page(case, [splash]) is None
    # 同一批页里，定位「推荐」的用例照常能推断出首页（证明不是把置备整体关掉）
    normal = TestCase(
        caseId="case_000002", name="首页渲染", target={"package": "com.x"},
        steps=[
            Step(id="s1", action=Action.LAUNCH),
            Step(id="s2", action=Action.TAP,
                 target=Locator(strategy=LocatorStrategy.TEXT, value="推荐"),
                 assertions=[Assertion(type=AssertionType.EXISTS, expected=None)]),
        ],
    )
    picked = infer_target_page(normal, [splash, home])
    assert picked is not None and picked["page_key"] == "home"


# -- 2026-10-21 补：根页目标 ≠ 断链 / launch 默认冷启动 -------------------------------------


class ColdAwareDriver(FakeNavDriver):
    """在 FakeNavDriver 上补 `app_stop` 计数（真实驱动都有，桩里此前缺）。"""

    def __init__(self, start: str = "A", navigate: bool = True) -> None:
        super().__init__(start=start, navigate=navigate)
        self.stops = 0

    def app_stop(self, package: str) -> None:
        self.stops += 1
        self.stack = []


def test_provision_root_target_cold_starts_instead_of_giving_up(tmp_path):
    """目标页就是**根页**时，不能报「父链不完整」——那只是"不需要导航"，仍须确认在根页。

    实测背景（2026-10-21 某生活服务 App同代 3 条 0/3）：用例首个动作的定位命中根页 ⇒ `_chain_of`
    返回 None（链为空）⇒ 旧实现报「父链不完整」**直接放弃，连冷启动都不做**，用例就在
    上一个用例遗留的深层页上开跑（截图里搜索框还留着上一轮的词），第 1 步定位失败，
    而错误信息只说"父链不完整"，把人引向图谱排查。
    """
    from insta_engine.arrival_provision import provision

    index_path = _build_graph(tmp_path)
    pages = load_index(index_path)["pages"]
    root = next(p for p in pages if not p.get("parent_key"))
    assert root.get("page_key")

    case = _case(("text", "去B"))          # 「去B」只在根页 A 上 ⇒ 目标 = 根页
    drv = ColdAwareDriver(start="C")       # 模拟上一个用例遗留的深层页
    ev = provision(case, drv, "com.x", index_path, sleep_scale=0.01)

    assert ev["reason"] != "父链不完整", ev
    assert ev["needed"] is True, ev
    assert drv.stops >= 1, "根页目标也必须冷启动，否则会从上个用例的遗留页开跑"
    assert drv.starts >= 1 and drv.stack[-1] == "A", "冷启动后应回到根页"
    assert ev["ok"] is True, ev


def test_provision_broken_chain_still_reports_incomplete(tmp_path):
    """真正的断链（目标有父页但链缺失）仍要如实报「父链不完整」，不能被上面的改动放过。"""
    from insta_engine.arrival_provision import provision

    index_path = _build_graph(tmp_path)
    index = load_index(index_path)
    deep = next(p for p in index["pages"] if p.get("parent_key"))
    deep["parent_key"] = "ghost"           # 父页不在图谱 ⇒ 真断链
    save_index(index_path, index)

    case = _case()
    drv = ColdAwareDriver(start="A")
    ev = provision(case, drv, "com.x", index_path, sleep_scale=0.01)
    assert ev["ok"] is False
    assert "父链不完整" in ev["reason"], ev


def test_launch_defaults_to_cold_start(monkeypatch, tmp_path):
    """`launch` 默认先 app_stop：热恢复会把上一个用例的遗留页当成本用例的起点。

    实测背景（2026-10-21）：某生活服务 App 3 条同代用例 0/3，截图显示 `launch` 后仍是上一轮的
    搜索结果页（搜索框里还留着上一轮的词）⇒ 第 1 步定位必然失败，而报错只说"定位失败"。
    """
    from insta_engine.runner import Runner

    idx = tmp_path / "com.x.json"
    idx.write_text("{}", encoding="utf-8")
    case = TestCase.model_validate(
        {"caseId": "c", "name": "n", "target": {"package": "com.x"},
         "steps": [{"id": "s0", "action": "launch"}]})

    monkeypatch.delenv("MAT_LAUNCH_COLD", raising=False)
    drv = ColdAwareDriver(start="C")
    Runner(drv, tmp_path, index_path=idx).run(case)
    assert drv.stops == 1, "默认应冷启动（app_stop 一次）"

    # 显式关掉：步骤参数优先
    drv2 = ColdAwareDriver(start="C")
    case2 = TestCase.model_validate(
        {"caseId": "c2", "name": "n", "target": {"package": "com.x"},
         "steps": [{"id": "s0", "action": "launch", "params": {"cold": "false"}}]})
    Runner(drv2, tmp_path, index_path=idx).run(case2)
    assert drv2.stops == 0

    # 环境变量关掉
    monkeypatch.setenv("MAT_LAUNCH_COLD", "0")
    drv3 = ColdAwareDriver(start="C")
    Runner(drv3, tmp_path, index_path=idx).run(case)
    assert drv3.stops == 0


def test_infer_prefers_shallowest_equivalent_fragment():
    """等价碎片里取**depth 最浅**的那条——否则会去重放一条通往广告的垃圾链。

    实测（2026-10-23 tx-news）：首页在图谱里有 15 条记录，其中一条挂在
    「首页 → 广告 → 广告 → back」上（depth=3）。按 hits/stable 选到那条，置备就会
    去走 5 跳、报「父链不完整」；而正确答案是「你已经在首页了，什么都不用做」。
    实测 006/007/008 三条用例的目标页全是这种副本。
    """
    from insta_engine.arrival_provision import infer_target_page

    # ⚠️ 必须带 `identities`：等价判据（`pages_equivalent`）比的是它，
    # 只给 controls 的话两页恒判"不等价"，测试会**假通过**。
    same = [{"text": "去C"}, {"text": "首页"}]
    ident = ["text:去C", "text:首页"]
    pages = [
        {"page_key": "home_root", "activity": "com.x/.A", "depth": 0, "hits": 2,
         "controls": same, "identities": ident},
        {"page_key": "home_deep_copy", "activity": "com.x/.A", "depth": 3, "hits": 9,
         "arrival": {"status": "stable"}, "controls": same, "identities": ident},
    ]
    page = infer_target_page(_case(("text", "首页")), pages)
    assert page is not None
    assert page["page_key"] == "home_root", (
        "hits 再多也不该选那条深链副本——置备成本是链长")
    assert page["depth"] == 0


def test_infer_does_not_collapse_different_pages_of_same_activity():
    """反向：**不同**逻辑页（内容不像）不能被"取最浅"合并掉——否则会走错页。"""
    from insta_engine.arrival_provision import infer_target_page

    pages = [
        {"page_key": "p_home", "activity": "com.x/.A", "depth": 0, "hits": 1,
         "controls": [{"text": "推荐"}, {"text": "要闻"}, {"text": "视频"}],
         "identities": ["text:推荐", "text:要闻", "text:视频"]},
        {"page_key": "p_detail", "activity": "com.x/.A", "depth": 2, "hits": 9,
         "controls": [{"text": "正文"}, {"text": "评论"}, {"text": "分享"}],
         "identities": ["text:正文", "text:评论", "text:分享"]},
    ]
    page = infer_target_page(_case(("text", "评论")), pages)
    assert page is not None and page["page_key"] == "p_detail"
