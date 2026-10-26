"""arrival 试跑验证测试：路径重放 N 轮，通过 ≥N-1 记 stable。"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine.arrivals import verify_arrivals
from insta_engine.page_index import load_index, merge_pages, new_index, page_key


class FakeExploreDriver:
    """A --去B--> B --去C--> C 状态机（与 test_v1.FakeExploreDriver 同构）。"""

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


def _build_graph(tmp_path: Path) -> Path:
    """先跑一轮 explore 沉淀 A/B/C 三层图谱（与真机流程一致）。"""
    from insta_engine.explore import explore

    index_path = tmp_path / "com.x.json"
    explore(FakeExploreDriver(), "com.x", minutes=0.05, max_steps=6,
            artifacts_dir=tmp_path / "art", max_depth=3, sleep_scale=0.01,
            index_path=index_path)
    return index_path


def test_arrivals_stable_paths(tmp_path):
    index_path = _build_graph(tmp_path)
    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    assert report["stable"] == 2 and report["verified"] == 2  # B、C 两条路径都稳定
    graph = load_index(index_path)
    pages = {p["page_key"]: p for p in graph["pages"]}
    arrivals = [p["arrival"] for p in pages.values() if p.get("depth", 0) >= 1]
    for entry in arrivals:
        assert entry["rounds"] == 3 and entry["passed"] == 3
        assert entry["status"] == "stable" and entry["success_rate"] == 1.0
        assert entry["entry"][0]["action"] == "launch"      # 复现路径自述
        assert all(hop["action"] == "tap" for hop in entry["entry"][1:])


def test_arrivals_unstable_when_flaky(tmp_path):
    """只有第一次点击真的换页 → 1/3 < 2（N-1 门槛）→ unstable。"""
    index_path = _build_graph(tmp_path)

    class FlakyDriver(FakeExploreDriver):
        def __init__(self):
            super().__init__()
            self.allowed = 1

        def click(self, x, y):
            self.clicks.append((self.stack[-1], x, y))
            if self.allowed > 0:
                self.allowed -= 1
                target = self.TRANS.get((self.stack[-1], x))
                if target:
                    self.stack.append(target)

    report = verify_arrivals(FlakyDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    b = min((r for r in report["results"] if r.get("status") in ("stable", "unstable")),
            key=lambda r: r["depth"])
    assert b["status"] == "unstable" and b["passed"] == 1 and b["rounds"] == 3
    assert "未达" in b["reason"]
    graph = load_index(index_path)
    b_page = next(p for p in graph["pages"] if p["depth"] == 1)
    assert b_page["arrival"]["status"] == "unstable"


def test_arrivals_label_missing_fails_honestly(tmp_path):
    """图谱记的文案在当前界面找不到（UI 改版）→ 失败并写明原因，不静默当稳定。

    改 A 页按钮文案（A 是起点、不校验 key）——若改 B 页，B 的 page_key 也变，
    第一跳会先报"落点不符"而非"未找到"，测不到目标分支。
    """
    index_path = _build_graph(tmp_path)

    class RenamedDriver(FakeExploreDriver):
        XML = dict(FakeExploreDriver.XML, A=FakeExploreDriver.XML["A"].replace("去B", "进入B"))

    report = verify_arrivals(RenamedDriver(), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    b = next(r for r in report["results"] if r["depth"] == 1)
    assert b["status"] == "unstable" and "未找到可点控件（文案「去B」）" in b["reason"]


def test_arrivals_broken_chain_skipped(tmp_path):
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [{"page_key": "orphan", "depth": 1, "parent_key": "ghost",
                         "parent_action": "某动作", "controls": []}])
    index_path.write_text(json.dumps(index), encoding="utf-8")

    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    assert report["verified"] == 0
    assert report["results"][0]["status"] == "skipped" and "父链不完整" in report["results"][0]["reason"]


def test_arrivals_click_budget_stops_early(tmp_path):
    index_path = _build_graph(tmp_path)
    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01, click_budget=1)
    assert report["stopped_early"] is True
    assert report["clicks"] == 1
    assert report["verified"] == 1  # 第一条路径只跑了 1 轮就被预算拦下


def test_arrivals_root_activity_mismatch_fails_honestly(tmp_path):
    """launch 落点与图谱根 activity 不同 → 如实拦下，且不烧点击。

    背景（2026-10-16 真机）：explore 把遗留页（播客详情页）当成根页，20 条链
    重放全军覆没，且全部误报成「第 1 跳未找到可点控件」——根页错位被跳级失败
    掩盖，排障多绕一圈。图谱记录 launch_activity 后在主循环**提前判 skip**
    （零冷启动成本）；探测不到 activity 时仍由走链前的根核对兜底。
    """
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "root", "depth": 0, "activity": "com.x/.HomeActivity", "controls": []},
        {"page_key": "child", "depth": 1, "parent_key": "root",
         "parent_action": "去C", "controls": []},
    ])
    index_path.write_text(json.dumps(index), encoding="utf-8")

    class LandedElsewhere(FakeExploreDriver):
        def foreground_app(self):
            return {"package": "com.x", "activity": "com.x/.OtherActivity"}

    # 主路径：探测拿到 launch 落点 ⇒ 主循环提前 skip（连冷启动试跑都省了）
    report = verify_arrivals(LandedElsewhere(), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    r = report["results"][0]
    assert r["status"] == "skipped" and "launch 不可达" in r["reason"]
    assert "HomeActivity" in r["reason"] and "OtherActivity" in r["reason"]
    assert report["clicks"] == 0
    # 探测结果已回写图谱（此后所有验证/修复共用这一基准事实）
    assert load_index(index_path).get("launch_activity") == "com.x/.OtherActivity"


def test_arrivals_root_check_fallback_when_launch_drifts(tmp_path):
    """兜底路径：图谱记录的 launch 落点可信（=根，判可达），但本次真机 launch
    漂移到别处 ⇒ 走链前的根核对拦截（fe74230 的场景在探测判据下仍保留）。"""
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "root", "depth": 0, "activity": "com.x/.HomeActivity", "controls": []},
        {"page_key": "child", "depth": 1, "parent_key": "root",
         "parent_action": "去C", "controls": []},
    ])
    index["launch_activity"] = "com.x/.HomeActivity"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    class LandedElsewhere(FakeExploreDriver):
        def foreground_app(self):
            return {"package": "com.x", "activity": "com.x/.OtherActivity"}

    report = verify_arrivals(LandedElsewhere(), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    r = report["results"][0]
    assert r["status"] == "unstable" and "根页不符" in r["reason"]
    assert "OtherActivity" in r["reason"]
    assert report["clicks"] == 0
    # 图谱已有 launch_activity ⇒ 不重探、不覆盖（历史记录保留）
    assert load_index(index_path).get("launch_activity") == "com.x/.HomeActivity"


def test_replay_force_stops_app_before_launch(tmp_path):
    """试跑每轮先 app_stop 再 app_start——app_start 对后台任务是热恢复，
    遗留页会被当成 launch 落点（与 explore 根锚定同口径，2026-10-16）。"""
    index_path = _build_graph(tmp_path)
    calls: list[str] = []

    class ColdDriver(FakeExploreDriver):
        def app_stop(self, package):
            calls.append("stop")

        def app_start(self, package):
            calls.append("start")
            super().app_start(package)

    verify_arrivals(ColdDriver(), "com.x", index_path, trials=2, sleep_scale=0.01)
    assert calls and calls[0] == "stop" and "start" in calls
    # 每次 start 之前都有一次 stop（不存在连续两次 start）
    assert all(not (a == b == "start")
               for a, b in zip(calls, calls[1:], strict=False))


def test_verify_arrivals_page_keys_filters_cohort(tmp_path):
    """`page_keys` 定向 cohort：只验证给定集合（否则浅链优先会吃掉全部预算，
    深层专项 cohort 轮不到——2026-10-16 真机实测踩过）。"""
    index_path = _build_graph(tmp_path)
    graph = load_index(index_path)
    keys = {p["page_key"] for p in graph["pages"] if p.get("depth") == 1}
    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01, page_keys=keys)
    assert {r["page_key"] for r in report["results"]} == keys
    assert report["verified"] == len(keys) == 1  # 只有 B（L1），C（L2）不在 cohort


def test_arrivals_transient_root_skipped_without_device(tmp_path):
    """链根是含「跳过」的瞬态 splash 帧 → 直接 skip 并说清原因，不烧真机。

    背景（2026-10-16 真机）：5 条 splash 根链重放 0/15 全死于「第 1 跳未找到
    可点控件（文案「跳过」）」——launch 后根帧被自动翻过，试跑是纯浪费。
    """
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "splash", "depth": 0, "activity": "com.x/.SplashActivity",
         "controls": [{"text": "跳过"}]},
        {"page_key": "child", "depth": 1, "parent_key": "splash",
         "parent_action": "跳过", "controls": []},
    ])
    index_path.write_text(json.dumps(index), encoding="utf-8")

    drv = FakeExploreDriver()
    report = verify_arrivals(drv, "com.x", index_path, trials=2, sleep_scale=0.01)
    r = report["results"][0]
    assert r["status"] == "skipped" and "瞬态启动帧" in r["reason"]
    assert report["clicks"] == 0
    assert drv.clicks == []  # 驱动零点击（冷启动都没发生）


def test_arrivals_unreachable_root_skipped_without_device(tmp_path):
    """根 activity ≠ launch 落点 → 直接 skip 并说清原因，不烧真机。

    背景（2026-10-16 真机）：tx-news 245 条链挂遗留播客页根，语义完整可重放，
    真机 0/20 全在根核对处拦下（还各烧 3 轮冷启动）——图谱有 launch_activity
    后应零成本提前判。
    """
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "legacy", "depth": 0, "activity": "com.x/.LegacyPage"},
        {"page_key": "child", "depth": 1, "parent_key": "legacy",
         "parent_action": "进入", "controls": []},
    ])
    index["launch_activity"] = "com.x/.OtherActivity"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    drv = FakeExploreDriver()
    report = verify_arrivals(drv, "com.x", index_path, trials=2, sleep_scale=0.01)
    r = report["results"][0]
    assert r["status"] == "skipped" and "launch 不可达" in r["reason"]
    assert "LegacyPage" in r["reason"] and "OtherActivity" in r["reason"]
    assert report["clicks"] == 0
    assert drv.clicks == []


def test_arrivals_probes_launch_activity_when_missing(tmp_path):
    """图谱缺 launch_activity ⇒ 冷启动探测一次并回写；根可达 ⇒ 正常试跑。"""
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "home", "depth": 0, "activity": "com.x/.SplashActivity"},
        {"page_key": "child", "depth": 1, "parent_key": "home",
         "parent_action": "进入", "controls": []},
    ])
    index_path.write_text(json.dumps(index), encoding="utf-8")

    class ProbeDriver(FakeExploreDriver):
        starts = 0

        def app_start(self, package):
            ProbeDriver.starts += 1
            self.stack = ["A"]

        def foreground_app(self):
            return {"package": "com.x", "activity": "com.x/.SplashActivity"}

    verify_arrivals(ProbeDriver(), "com.x", index_path,
                    trials=3, sleep_scale=0.01)
    # 探测回写进图谱（此后所有验证/修复共用这一基准事实）
    assert load_index(index_path).get("launch_activity") == "com.x/.SplashActivity"
    # 探测 1 次 + child 链 1 跳 × 3 轮稳定试跑
    assert ProbeDriver.starts == 1 + 3


def test_arrivals_probe_failure_stays_silent(tmp_path):
    """驱动取不到 activity ⇒ 探测返回空 ⇒ 不回写、判据不表态（旧行为）。"""
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "home", "depth": 0, "activity": "com.x/.A"},
        {"page_key": "child", "depth": 1, "parent_key": "home",
         "parent_action": "去B", "controls": []},
    ])
    index_path.write_text(json.dumps(index), encoding="utf-8")

    # FakeExploreDriver 无 foreground_app ⇒ 探测拿不到 activity
    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    assert "launch_activity" not in load_index(index_path)
    # 判据不表态 → 不误杀：这里链本身不完整（child 控件空链按父链重放会失败），
    # 只断言没有「launch 不可达」类的误报原因
    for r in report["results"]:
        assert "launch 不可达" not in (r.get("reason") or "")


def test_dismiss_splash_then_walk_chain(tmp_path):
    """冷启动先落开屏帧（含「跳过」）→ 前置翻掉 → 从首页起链可重放。

    tx-news 实测（2026-10-16）：冷启动必经开屏帧且与首页同 activity，不翻掉
    的话走链第①跳在广告帧上找控件必死（0/15）。开屏点击计入预算（诚实）。
    """
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "home", "depth": 0, "activity": "com.x/.SplashActivity"},
    ])
    # A--去B-->B：child = B 页（A 页的可点控件是「去B」）
    child_key = page_key(FakeExploreDriver.XML["B"], "com.x")
    merge_pages(index, [{"page_key": child_key, "depth": 1, "parent_key": "home",
                         "parent_action": "去B", "controls": []}])
    index_path.write_text(json.dumps(index), encoding="utf-8")

    SPLASH_XML = ('<node class="TextView" text="广告" package="com.x"/>'
                  '<node class="Button" text="跳过" clickable="true" '
                  'bounds="[900,80][990,140]" package="com.x"/>')

    class SplashDriver(FakeExploreDriver):
        """冷启动先落 SPLASH 帧；点「跳过」（中心 945,110）后回到 A。"""

        def __init__(self):
            super().__init__()
            self.stack = ["A"]

        def app_start(self, package):
            self.stack = ["SPLASH"]

        def dump(self):
            if self.stack[-1] == "SPLASH":
                return SPLASH_XML
            return self.XML[self.stack[-1]]

        def click(self, x, y):
            if self.stack[-1] == "SPLASH" and x == 945:
                self.stack = ["A"]     # 跳过 → 首页
                return
            super().click(x, y)

    report = verify_arrivals(SplashDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    assert report["stable"] == 1 and report["verified"] == 1


def test_dismiss_splash_counts_clicks_into_budget():
    """开屏「跳过」的点击计入 state（预算诚实），不计则预算口径漂移。"""
    from insta_engine.arrivals import _dismiss_splash

    SPLASH_XML = ('<node class="Button" text="跳过" clickable="true" '
                  'bounds="[900,80][990,140]" package="com.x"/>')

    class SplashOnly:
        def dump(self):
            return SPLASH_XML          # 一直停在开屏帧（点不动，等超时）

        def click(self, x, y):
            pass

    state = {"clicks": 0}
    _dismiss_splash(SplashOnly(), "com.x", 0.01, state, max_wait=0.05)
    assert state["clicks"] >= 1        # 点过跳过（哪怕帧没翻过去）


# -- 内容轮换：多轮碎片互证（2026-10-17 真机 btn_listener 链实测的闭环）-----------------------------


def test_content_drift_fragments_upgraded_to_stable(tmp_path):
    """终点落点每轮是不同内容快照（page_key 不同）但 rid 结构相同、activity 一致
    ⇒ 多轮互证 → stable，reason 注明碎片互证；不写图谱、不放宽单轮判据。"""
    index_path = _build_graph(tmp_path)

    class DriftDriver(FakeExploreDriver):
        """A→B 落点每次注入不同 text（内容轮换快照），B 结构 rid 不变。"""

        XML = dict(FakeExploreDriver.XML, **{
            "B": ('<node class="Button" text="去C" resource-id="com.x:id/btn_c" '
                  'clickable="true" bounds="[0,0][10,10]" package="com.x"/>'),
        })

        def __init__(self):
            super().__init__()
            self.gen = 0

        def app_start(self, package):
            self.stack = ["A"]
            self.gen += 1

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
            return {"package": "com.x", "activity": "com.x/.MainActivity"}

    report = verify_arrivals(DriftDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    entry = report["results"][0]
    assert entry["status"] == "stable", entry
    assert "互证" in (entry.get("reason") or "")


def test_content_drift_not_aligned_when_activities_differ(tmp_path):
    """落点 activity 漂移（真乱跳）⇒ 不互证，维持 unstable。"""
    index_path = _build_graph(tmp_path)

    class JumpDriver(FakeExploreDriver):
        def __init__(self):
            super().__init__()
            self.gen = 0

        def app_start(self, package):
            self.stack = ["A"]
            self.gen += 1

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
            return {"package": "com.x",
                    "activity": f"com.x/.Activity{self.gen}"}   # 每轮 activity 都变

    report = verify_arrivals(JumpDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    entry = report["results"][0]
    assert entry["status"] == "unstable"
    assert "落点不符" in (entry.get("reason") or "")


def test_waypoint_drift_pass(tmp_path):
    """途经页（L2 链的第①跳落点 B）每轮内容快照不同：rid 结构相同 + activity
    一致 ⇒ 单轮在线放行，链走完到 C，终点精确匹配 → stable（无需互证）。"""

    class WaypointDriftDriver(FakeExploreDriver):
        """B 页带结构 rid，落点附加每轮不同的 text（内容快照轮换）。"""

        XML = {**FakeExploreDriver.XML,
               "B": ('<node class="Button" text="去C" resource-id="com.x:id/btn_c" '
                     'clickable="true" bounds="[0,0][10,10]" package="com.x"/>')}

        def __init__(self):
            super().__init__()
            self.gen = 0

        def app_start(self, package):
            self.stack = ["A"]
            self.gen += 1

        def dump(self):
            if self.stack[-1] == "B":
                return (self.XML["B"]
                        + f'<node class="TextView" text="快照{self.gen}" package="com.x"/>')
            return self.XML[self.stack[-1]]

        def foreground_app(self):
            return {"package": "com.x", "activity": "com.x/.MainActivity"}

    # 建图与验证同一 driver：图谱 B 页才带 rid 信号与 activity（途经判据的基准）
    from insta_engine.explore import explore
    index_path = tmp_path / "com.x.json"
    explore(WaypointDriftDriver(), "com.x", minutes=0.05, max_steps=6,
            artifacts_dir=tmp_path / "art", max_depth=3, sleep_scale=0.01,
            index_path=index_path)

    report = verify_arrivals(WaypointDriftDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    c = next(e for e in report["results"] if e["depth"] == 2)
    assert c["status"] == "stable", c          # 途经 B 轮换被放行，终点 C 稳定


def test_waypoint_drift_not_pass_when_rids_differ(tmp_path):
    """途经落点 rid 结构不同（真跳到了别的页）⇒ 不放行，判落点不符。"""

    class WrongPageDriver(FakeExploreDriver):
        XML = {**FakeExploreDriver.XML,
               "B": ('<node class="Button" text="去C" resource-id="com.x:id/btn_c" '
                     'clickable="true" bounds="[0,0][10,10]" package="com.x"/>'),
               "D": '<node class="TextView" text="D页面" package="com.x"/>'}

        def __init__(self, wrong=False):
            super().__init__()
            self.wrong = wrong                 # 验证期 A 点击改落 D（无 rid 结构）

        def click(self, x, y):
            if self.wrong and self.stack[-1] == "A":
                self.clicks.append((self.stack[-1], x, y))
                self.stack.append("D")
                return
            super().click(x, y)

        def foreground_app(self):
            return {"package": "com.x", "activity": "com.x/.MainActivity"}

    from insta_engine.explore import explore
    index_path = tmp_path / "com.x.json"
    explore(WrongPageDriver(), "com.x", minutes=0.05, max_steps=6,
            artifacts_dir=tmp_path / "art", max_depth=3, sleep_scale=0.01,
            index_path=index_path)

    report = verify_arrivals(WrongPageDriver(wrong=True), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    c = next(e for e in report["results"] if e["depth"] == 2)
    assert c["status"] == "unstable"
    assert "落点不符" in (c.get("reason") or "")


# -- 内容型文案跳（2026-10-17 真机 0/8 复盘：无 rid 长文案重放必挂，零成本 skip）--


def test_arrivals_content_label_hop_skipped_without_device(tmp_path):
    """链含内容型文案跳（无 rid 长文案）→ 零成本 skip，不烧真机。

    背景（2026-10-17 真机）：tx-news 8 条链 24 轮 0 通过、clicks=2——全部死于
    第 1 跳内容文案（「MORIN 法式撞色…」等推荐流标题）随内容轮换找不到控件。
    """
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [
        {"page_key": "root", "depth": 0, "activity": "com.x/.HomeActivity"},
        {"page_key": "child", "depth": 1, "parent_key": "root",
         "parent_action": "MORIN 法式撞色条纹开叉上衣皮衣半裙套装女秋2",
         "parent_rid": "", "controls": []},
    ])
    index["launch_activity"] = "com.x/.HomeActivity"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    drv = FakeExploreDriver()
    report = verify_arrivals(drv, "com.x", index_path, trials=2, sleep_scale=0.01)
    r = report["results"][0]
    assert r["status"] == "skipped" and "内容型文案跳" in r["reason"]
    assert report["clicks"] == 0 and drv.clicks == []  # 冷启动都没发生

