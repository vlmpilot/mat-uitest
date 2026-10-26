"""增量探索（跨轮 `tried` 复用）的**确定性**验证：把"接线"与"key 稳定性"两个因素分开。

**要回答的问题**：
    第 2 轮探索会不会跳过第 1 轮点过的分支？也就是"越探越深"这个自称收益最大的功能，
    当前到底成不成立？

**为什么用假驱动而不是真机**：真机跑两轮是**不可复现**的（App 状态、风控、设备管理都在变），
而且分不清失败原因。假驱动可以把两个因素**单独打开**：

    test_reuse_works_when_page_key_is_stable  → 只测接线：key 稳定时，第 2 轮必须命中第 1 轮的 tried
    test_reuse_is_lost_when_page_key_churns   → 只测 key 不稳的后果：res-id 抖动时，命中率归零

两条一起才构成完整结论：**接线是对的，失效的唯一原因是 `page_key` 不稳**。
真实数据上的规模由 `test_page_identity_corpus.py` 的跨轮命中率指标给出（当前 10%）。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine.explore import explore


class _Counter:
    """全局自增计数：用来制造"每次 dump 都不一样"的 res-id。"""

    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _xml(page: str, counter: _Counter, churn: bool) -> str:
    """A 页有一个可点按钮「去B」；B 页无可点控件。

    `churn` 控制 res-id 的抖动程度（真实 App 上三种都存在）：

    - `"none"`    全部稳定 —— 老路径（`page_key` 精确匹配）就该生效；
    - `"partial"` **部分稳定 + 部分抖动** —— **真实 App 的常态**（实测同页包含度中位 0.70）。
                  此时 `page_key` 每次都变（精确匹配必然失效），但信号集合仍有交集 ⇒
                  只能靠**包含度认页**救回来；
    - `"full"`    全部抖动 —— 最坏情况，交集为空，包含度也救不了。用来标出兜底路径的边界。
    """
    if churn == "full":
        rid = f"com.x:id/n{counter.next()}"
    elif churn == "partial":
        rid = f"com.x:id/churn{counter.next()}"
    else:
        rid = "com.x:id/stable"
    if page == "A":
        extra = ""
        if churn == "partial":
            # 一个**稳定**的容器 rid + 一段稳定文案：真实页面上这类信号占多数，
            # 抖动的只是少数（轮播/角标）。包含度就是靠它们认页的。
            extra = ('<node class="View" text="" resource-id="com.x:id/root" clickable="false"'
                     ' bounds="[0,0][100,100]" package="com.x"/>'
                     '<node class="TextView" text="稳定的标题" resource-id="" clickable="false"'
                     ' bounds="[0,0][100,20]" package="com.x"/>')
        return (f'<node class="Button" text="去B" resource-id="{rid}" clickable="true"'
                f' bounds="[0,0][10,10]" package="com.x"/>{extra}')
    return (f'<node class="TextView" text="B页" resource-id="{rid}"'
            f' bounds="[0,0][10,10]" package="com.x"/>')


class FakeDriver:
    """A --去B--> B 的两页状态机（与 test_v1 / test_arrivals 的假驱动同构）。"""

    serial = None
    raw = None

    def foreground_app(self) -> dict:
        """P0②：引擎已改走中立方法（不再读 drv.raw.app_current）。"""
        return (self.raw.app_current() or {}) if self.raw else {}

    def __init__(self, counter: _Counter, churn: str) -> None:
        self.counter = counter
        self.churn = churn
        self.stack = ["A"]
        self.clicks: list[tuple[str, int, int]] = []

    def dump(self) -> str:
        return _xml(self.stack[-1], self.counter, self.churn)

    def click(self, x: int, y: int) -> None:
        self.clicks.append((self.stack[-1], x, y))
        if self.stack[-1] == "A":
            self.stack.append("B")

    def press(self, key: str) -> None:
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.stack = ["A"]


def _run_twice(tmp_path: Path, churn: str) -> tuple[dict, dict]:
    """同一个 index_path 跑两轮，返回两轮的 report。

    每轮用**各自的** artifacts 目录（真实运行也是每次一个新目录）；共用一个目录会让
    `mkdir(exist_ok=True)` 在沙箱下抛 EEXIST。
    """
    counter = _Counter()
    index = tmp_path / "com.x.json"
    def run(tag: str) -> dict:
        return explore(FakeDriver(counter, churn), "com.x", minutes=0.02, max_steps=6,
                       artifacts_dir=tmp_path / tag, max_depth=3,
                       sleep_scale=0.01, index_path=index)
    return run("r1"), run("r2")


def test_reuse_works_when_page_key_is_stable(tmp_path: Path) -> None:
    """接线是对的：`page_key` 稳定时，第 2 轮必须**真正跳过**第 1 轮记下的分支。

    这一条把"接线坏了"从怀疑名单里排除掉——否则跨轮复用失效会被误判成
    `index_path` 没接上、`merge_pages` 没存 tried 之类的别的原因。
    """
    r1, r2 = _run_twice(tmp_path, churn="none")
    assert r1["reused_tried"] == 0 and r1["skipped_by_history"] == 0, "第 1 轮不该有可复用的历史"
    assert r1["pages"], "第 1 轮应至少发现 1 页"
    assert r2["prior_pages"], "第 2 轮应读回第 1 轮沉淀的图谱（接线断了）"
    assert r2["skipped_by_history"] >= 1, (
        f"key 稳定时第 2 轮仍未真正跳过任何已试分支"
        f"（读回 {r2['reused_tried']} 条、跳过 {r2['skipped_by_history']} 个）"
        f"——说明是接线问题，不是 page_key 的问题")


def test_reuse_survives_partial_churn_via_containment(tmp_path: Path) -> None:
    """**真实 App 的常态**：部分信号稳定、部分抖动 —— 必须靠**包含度认页**救回增量。

    这是 2026-10-01 修的核心场景。`page_key` 每次都变（精确键匹配必然失效），
    但页面上多数信号是稳的 ⇒ 信号集合仍有交集 ⇒ 按包含度能认出"这是同一页"。

    修复前这里是 `skipped_by_history == 0`（整轮增量失效）；修复后必须 > 0。
    """
    r1, r2 = _run_twice(tmp_path, churn="partial")
    assert r1["reused_tried"] == 0, "第 1 轮不该有可复用的历史"
    assert r2["prior_pages"], "第 2 轮应读回图谱"
    assert r2["reused_tried"] > 0, "应读回了历史 tried（否则是夹具没造对）"
    assert r2["skipped_by_history"] > 0, (
        "部分抖动下第 2 轮仍未跳过任何分支——包含度认页没生效。"
        "这正是修复前的老毛病：page_key 精确匹配失效 ⇒ 增量探索整轮白跑")


def test_reuse_survives_full_churn_via_structural_signals(tmp_path: Path) -> None:
    """**全部信号都抖动**时的兜底（2026-10-22 起）：靠**结构信号**认回同一页。

    这条原来断言"增量归零"（`skipped_by_history == 0`），理由是"页面上一个稳定信号都没有
    就无解"。某内容社区 App实测把这个前提推翻了：内容页的文案全是动态的、res-id 又全混淆，
    稳定信号只剩 1~3 个 —— 于是 153 页里 17 页只有 1 个信号、两两近重复 1112 组，
    探索"每帧一个新页"，深度爆炸后 `stuck`。

    现在 `identities_from_controls` 在信号稀薄（<3）时补一层**与内容无关**的结构信号
    （控件类名 + 是否可点）⇒ 全抖动也能认回同一页，增量不再归零。

    ⚠️ 仍然锁住那个**指标陷阱**：`reused_tried`（读回数）与 `skipped_by_history`（真跳过数）
    必须分开看——只报前者会把"历史一条没用上"说成"复用了 N 个分支"（CLI 原先正是这么写的）。
    """
    r1, r2 = _run_twice(tmp_path, churn="full")
    assert r1["steps"] >= 1, "第 1 轮应至少点一次"
    assert r2["prior_pages"], "图谱本身是读回来了的"
    assert r2["reused_tried"] > 0, (
        "res-id 抖动时应当**读回**了历史（图谱里有 tried），读回数为 0 说明夹具没造对")
    assert r2["skipped_by_history"] > 0, (
        "全抖动时第 2 轮仍未跳过任何分支——结构信号兜底没生效"
        f"（读回 {r2['reused_tried']} 条 / 跳过 {r2['skipped_by_history']} 个）")


def test_structural_fallback_does_not_fire_when_text_is_rich() -> None:
    """**反向**：文案充足的页面不许被结构信号稀释（否则同布局不同标题的页会被误并）。

    判据是"只在稳定信号 < 3 时补"。这条守住阈值，防止有人把兜底改成无条件生效。
    """
    from insta_engine.page_index import identities_from_controls

    rich = [{"text": f"设置项{i}", "class": "android.widget.TextView", "clickable": True}
            for i in range(4)]
    assert not any(sig.startswith("cls:") for sig in identities_from_controls(rich))

    thin = [{"text": "", "class": "android.widget.TextView", "clickable": True},
            {"text": "", "class": "android.widget.ImageView", "clickable": True}]
    sigs = identities_from_controls(thin)
    assert sigs and all(sig.startswith("cls:") for sig in sigs), sigs


# -- 外部评审 P0-3：mat index 建的图谱必须也带 identities（首次使用主路径兜底） -------

def test_index_built_graph_enables_containment_reuse(tmp_path: Path) -> None:
    """用户主路径 = mat index → mat explore。crawl 落盘的页面没有 identities，
    修复前包含度认页在该路径上静默失效；修复后 _normalize_page 就地补算，
    page_key 抖动场景下第 2 轮必须真正跳过已试分支。"""
    from insta_engine.page_index import load_or_crawl

    counter = _Counter()
    index_path = tmp_path / "com.x.json"
    # mat index 同代码路径：load_or_crawl 建图落盘（页面经 _normalize_page 回填 identities）
    load_or_crawl(FakeDriver(counter, "partial"), "com.x", index_path,
                  max_steps=6, max_pages=4)

    r2 = explore(FakeDriver(counter, "partial"), "com.x", minutes=0.02, max_steps=8,
                 artifacts_dir=tmp_path / "r2", max_depth=3, sleep_scale=0.01,
                 index_path=index_path)
    assert r2["skipped_by_history"] > 0, (
        f"mat index 建图 + page_key 抖动场景下第 2 轮仍未跳过任何分支"
        f"（读回 {r2['reused_tried']}、跳过 {r2['skipped_by_history']}）——identities 回填未生效")


# -- 选点策略开关（没有对照就答不了"启发式有没有让探索变好"） --------------

def test_heuristic_switch_is_read_at_runtime(monkeypatch):
    """`MAT_HEURISTIC` 必须**每次读**，不能在模块级缓存。

    这是 R2 的教训复用：`GEN_JSON_REPAIRS` 曾在模块级 `int(os.environ...)`，
    于是 `.env` 里设了不生效、运行中改也不生效。这里直接用"改环境变量立刻生效"来锁住它。
    """
    from insta_engine.explore import heuristic_enabled

    monkeypatch.setenv("MAT_HEURISTIC", "0")
    assert heuristic_enabled() is False, "MAT_HEURISTIC=0 应关闭"
    monkeypatch.setenv("MAT_HEURISTIC", "1")
    assert heuristic_enabled() is True, "改回 1 后必须立刻生效（说明没有 import 时冻结）"
    monkeypatch.delenv("MAT_HEURISTIC", raising=False)
    assert heuristic_enabled() is True, "缺省应为开启（不能悄悄退化成对照组）"
    for off in ("false", "No", "OFF"):
        monkeypatch.setenv("MAT_HEURISTIC", off)
        assert heuristic_enabled() is False, f"{off} 应被识别为关闭"


def test_heuristic_mode_is_recorded_in_report(tmp_path, monkeypatch):
    """报告必须记录本轮用的是哪种选点——否则对照实验的产物事后分不清谁是谁。"""
    monkeypatch.setenv("MAT_HEURISTIC", "0")
    r = explore(FakeDriver(_Counter(), "none"), "com.x", minutes=0.02, max_steps=4,
                artifacts_dir=tmp_path / "off", max_depth=3, sleep_scale=0.01)
    assert r["heuristic"] is False

    monkeypatch.delenv("MAT_HEURISTIC", raising=False)
    r = explore(FakeDriver(_Counter(), "none"), "com.x", minutes=0.02, max_steps=4,
                artifacts_dir=tmp_path / "on", max_depth=3, sleep_scale=0.01)
    assert r["heuristic"] is True


def test_skip_rate_denominator_is_same_kind_as_numerator(tmp_path):
    """跳过率的分母必须是**同口径**的"本轮检查过的候选"，不能用全图谱读回量。

    那个指标陷阱的翻版：`reused_tried` 是**全图谱**的已试分支数，
    而跳过只发生在**本轮走到的**页面上——拿它做分母会算出"跳过率 7/96"这种没有意义的数。
    这里锁住：`skip_rate == skipped / considered`，且 `considered >= skipped`。
    """
    counter = _Counter()
    index_path = tmp_path / "com.x.json"

    def run(tag: str) -> dict:
        return explore(FakeDriver(counter, "partial"), "com.x", minutes=0.02,
                       max_steps=6, artifacts_dir=tmp_path / tag, max_depth=3,
                       sleep_scale=0.01, index_path=index_path)

    run("r1")
    r2 = run("r2")
    assert r2["candidates_considered"] >= r2["skipped_by_history"] >= 0
    if r2["candidates_considered"]:
        assert abs(r2["skip_rate"] - r2["skipped_by_history"] / r2["candidates_considered"]) < 1e-3
    # ⚠️ 这里原来断言 `skip_rate == 1.0`，**那其实是"同坐标不同控件被误跳"的症状**
    # （2026-10-22 查出来）：该夹具把三个不同控件都放在 `[0,0][10,10]` 上，而旧 `tried`
    # 只记坐标 ⇒ 一个都没真试过的控件也因"坐标撞了"被跳过。现在 `tried` 带语义
    # （`bounds\tlabel\trid`，要求 label/rid 至少一个相同），这类撞坐标**不再跳**。
    # 所以正确的断言是"口径一致 + 确有跳过 + 不是全跳"。
    assert 0 < r2["skip_rate"] < 1.0, (
        f"第 2 轮应只跳过真正试过的分支（不是 0，也不该是 1.0=撞坐标全跳）："
        f"{r2['skipped_by_history']}/{r2['candidates_considered']} = {r2['skip_rate']}")


# -- 分级回退 + 系统弹窗识别 ------------------------------

class _Raw:
    """最小 `drv.raw` 桩：只提供 `app_current()`。"""

    def __init__(self, package: str, activity: str = "X") -> None:
        self._pkg, self._act = package, activity

    def app_current(self) -> dict:
        return {"package": self._pkg, "activity": self._act}


def test_system_dialog_is_not_counted_as_leaving_app():
    """权限弹窗等**系统 UI** 不能被当成"被测 App 跳走了"。

    实测（2026-10-02）：某新闻类 App 弹出「是否允许录制音频？」权限弹窗挡住 App，
    `back` 关不掉、`app_start` 也被盖住 ⇒ 一轮 **50 次"离场"、0 步、0 页**，
    而且报告里只有 "left-app"，看不出真实原因。
    """
    from insta_engine.explore import is_system_dialog

    for pkg in ("com.android.permissioncontroller",
                "com.android.packageinstaller",
                "com.android.systemui"):
        assert is_system_dialog(pkg), f"{pkg} 应被判为系统 UI"
    # 反例：系统设置本身是**合法的被测目标**（spike 的 t0 任务就是它），不能算系统弹窗
    for pkg in ("com.android.settings", "com.tencent.news", "", "com.huawei.android.launcher"):
        assert not is_system_dialog(pkg), f"{pkg!r} 不该被判为系统弹窗"


def test_blocked_by_system_dialog_terminates_instead_of_spinning(tmp_path):
    """被系统弹窗挡住时必须**终止本轮**，而不是转到烧完预算。

    这是"回退不了就终止"在**系统弹窗**场景下的直接价值：
    修复前会一路转到 50 次"离场"；修复后应在上限内停下，并给出可行动的原因。
    """
    from insta_engine.explore import MAX_LEFT_STREAK

    class BlockedDriver(FakeDriver):
        raw = _Raw("com.android.permissioncontroller", "GrantPermissionsActivity")

        def app_start(self, package: str) -> None:      # 弹窗盖住 App ⇒ 冷启动也回不去
            pass

        def press(self, key: str) -> None:              # back 关不掉权限弹窗
            pass

    r = explore(BlockedDriver(_Counter(), "none"), "com.x", minutes=0.05, max_steps=30,
                artifacts_dir=tmp_path / "b", max_depth=3, sleep_scale=0.01)

    assert r["steps"] == 0, "被挡住时不可能点出任何一步"
    assert r["left_app"] <= MAX_LEFT_STREAK + 1, (
        f"应在 {MAX_LEFT_STREAK} 次内终止，实际离场 {r['left_app']} 次——又空转了")
    kinds = {f["type"] for f in r["findings"]}
    assert "system-dialog" in kinds, f"必须把原因记成 system-dialog（实际 {kinds}）"
    assert "blocked" in kinds, f"必须记一条 blocked 说明已终止本轮（实际 {kinds}）"
    # 原因要**可行动**：能看出是弹窗而不是"App 自己跳走"
    detail = " ".join(f["detail"] for f in r["findings"] if f["type"] == "system-dialog")
    assert "权限" in detail or "弹窗" in detail, f"detail 应指出是系统弹窗：{detail}"


def test_fallback_once_verifies_before_after_not_a_stored_tree():
    """回退必须**校验"回退前 vs 回退后"**，不能拿存起来的树去比（否则校验恒真）。

    refactored 旧版就栽在这里（`site_crawler.py:717`：用**父页自己的树**做校验 ⇒ 恒真）。
    这里用"前台包名是否回到被测 App"做校验，语义明确、无法恒真。
    """
    from insta_engine.explore import fallback_once

    class OkDriver(FakeDriver):
        raw = _Raw("com.x")            # 回退后前台就是被测 App

    class StillOutDriver(FakeDriver):
        raw = _Raw("com.other.app")    # 回退后仍在外来 App

    assert fallback_once(OkDriver(_Counter(), "none"), "com.x", 1, 0.01) is True
    assert fallback_once(StillOutDriver(_Counter(), "none"), "com.x", 1, 0.01) is False

    class UnknownDriver(FakeDriver):
        raw = None                     # 取不到前台 ⇒ 按"未知"放行，不该升级到更重的回退

    assert fallback_once(UnknownDriver(_Counter(), "none"), "com.x", 1, 0.01) is True


# -- 选点质量：测**决策**而不是端到端页数 ----------------------------

def test_pick_prefers_entry_to_unseen_page():
    """启发式①：应优先点**通向未见/少访问页面**的入口。

    ⚠️ **为什么不靠真机 A/B 测这个**：真机端到端页数的**噪声地板高达 2.75×**
    （2026-10-02 实测：稳定目标 + 同一处理连跑 3 轮，页数 [11, 4, 11]，CV 81%）
    ——比任何合理的启发式效应都大，n=2 的对照根本测不出来。
    而"选点"是**单步决策**，不受下游路径依赖放大 ⇒ **在这里测，确定性、零成本、可进 CI**。
    真机只用来验"整体是否变好"，那需要很多次重复才谈得上统计意义。
    """
    from insta_engine.explore import _pick

    cands = [{"label": "老分支", "bounds": "[0,0][10,10]"},
             {"label": "新分支", "bounds": "[0,20][10,30]"},
             {"label": "没文案", "bounds": "[0,40][10,50]"}]
    # 图谱：当前页有一条边「新分支 → 未见页（hits 不在表里 ⇒ 视为 0 次访问）」
    edges = {"cur": [("新分支", "never_seen")]}
    hits = {"cur": 5, "old": 9}

    picked = _pick(cands, {"cur"}, 0, edges, hits)
    assert picked["label"] == "新分支", (
        f"应优先点通向未见页面的入口，实际选了 {picked['label']!r}")

    # 对照组（MAT_HEURISTIC=0 走的纯轮转）：step=0 应选第一个 —— 与启发式**选择不同**
    assert cands[0 % len(cands)]["label"] == "老分支", "夹具前提：轮转在 step=0 选第一个"


def test_pick_falls_back_to_label_then_roundrobin():
    """没有图谱信息时退化为「有文案优先 + 轮转」——这是"无图谱也能用"的保证。"""
    from insta_engine.explore import _pick

    cands = [{"label": "", "bounds": "[0,0][10,10]"},       # 图片按钮，无文案
             {"label": "有文案", "bounds": "[0,20][10,30]"}]
    assert _pick(cands, set(), 0, {}, {})["label"] == "有文案", "无图谱时应有文案优先"
    # 全都没文案 ⇒ 纯轮转（同分取 step % len）
    plain = [{"label": "", "bounds": "[0,0][10,10]"},
             {"label": "", "bounds": "[0,20][10,30]"}]
    assert _pick(plain, set(), 1, {}, {})["bounds"] == "[0,20][10,30]"


# -- 开关类控件：点它会改设备状态 ⇒ 改掉下一轮的前提（尾巴） ----------------

def test_candidate_exposes_toggle_and_can_skip_it():
    """`_candidate` 要标出**开关类**控件，且 `skip_switches=True` 时能排除它们。

    为什么：点开关**不产生新页面，却会改设备状态**，从而改掉下一轮运行的前提。
    实测（系统设置）：拨一次蓝牙 ⇒ 下一轮蓝牙页 11→34 个控件 ⇒ 页数 `[10,7,10,7,10,7]` 严格交替。
    """
    from insta_engine.explore import _candidate, _is_toggle_class

    xml = ('<node class="android.widget.Switch" text="蓝牙" resource-id="x:s"'
           ' clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
           '<node class="android.widget.Button" text="更多" resource-id="x:b"'
           ' clickable="true" bounds="[0,10][10,20]" package="com.x"/>')

    cands = _candidate(xml, "com.x")
    by_label = {c["label"]: c for c in cands}
    assert by_label["蓝牙"]["toggle"] is True, "Switch 应被标为开关"
    assert by_label["更多"]["toggle"] is False, "Button 不该被标为开关"

    skipped = _candidate(xml, "com.x", skip_switches=True)
    assert [c["label"] for c in skipped] == ["更多"], f"开关应被排除，实际 {[c['label'] for c in skipped]}"

    for cls in ("android.widget.Switch", "android.widget.CheckBox",
                "android.widget.ToggleButton", "android.widget.CompoundButton"):
        assert _is_toggle_class(cls), f"{cls} 应判为开关类"
    for cls in ("android.widget.Button", "android.widget.TextView", ""):
        assert not _is_toggle_class(cls), f"{cls!r} 不该判为开关类"


def test_skip_switches_switch_is_read_at_runtime(monkeypatch):
    """`MAT_SKIP_SWITCHES` 必须**每次读**（同 R2 的教训，不在模块级缓存）。"""
    from insta_engine.explore import skip_switches_enabled

    monkeypatch.delenv("MAT_SKIP_SWITCHES", raising=False)
    assert skip_switches_enabled() is False, "缺省必须**不跳**开关——不能悄悄改行为"
    monkeypatch.setenv("MAT_SKIP_SWITCHES", "1")
    assert skip_switches_enabled() is True
    monkeypatch.setenv("MAT_SKIP_SWITCHES", "0")
    assert skip_switches_enabled() is False, "改回 0 必须立刻生效（说明没被冻结）"


def test_toggle_clicks_are_counted_and_can_be_skipped(tmp_path, monkeypatch):
    """点开关要**计数**（>0 说明本轮改了设备状态）；`MAT_SKIP_SWITCHES=1` 时不点。"""
    class ToggleOnlyDriver(FakeDriver):
        """整页只有一个**开关**候选；点它**不换页**（只改状态）。"""

        def dump(self) -> str:
            return ('<node class="android.widget.Switch" text="蓝牙" resource-id="x:s"'
                    ' clickable="true" bounds="[0,0][10,10]" package="com.x"/>')

        def click(self, x: int, y: int) -> None:
            self.clicks.append(("A", x, y))          # 刻意不换页

    monkeypatch.delenv("MAT_SKIP_SWITCHES", raising=False)
    r = explore(ToggleOnlyDriver(_Counter(), "none"), "com.x", minutes=0.03, max_steps=6,
                artifacts_dir=tmp_path / "on", max_depth=3, sleep_scale=0.01)
    assert r["toggle_clicks"] >= 1, "默认应点开关（不悄悄改行为）"
    assert r["skip_switches"] is False
    assert r["start_page_key"], "报告必须记录起始页 key（多次运行的极差可能混了'前提不同'）"

    monkeypatch.setenv("MAT_SKIP_SWITCHES", "1")
    r2 = explore(ToggleOnlyDriver(_Counter(), "none"), "com.x", minutes=0.03, max_steps=6,
                 artifacts_dir=tmp_path / "skip", max_depth=3, sleep_scale=0.01)
    assert r2["toggle_clicks"] == 0, "开关被跳过时不该点它"
    assert r2["skip_switches"] is True


# -- 耗尽检测：探完了就停，而不是空转到预算耗尽 ------------------------

def test_stop_on_exhausted_is_opt_in_and_off_by_default(tmp_path, monkeypatch):
    """`MAT_STOP_EXHAUSTED` 缺省**必须关**——实测证明它不该默认开。

    真机对照（系统设置、同配置、给足预算）：开着时页数 `[9, 24, 9, 12]`（极差 2.67×、CV **111%**），
    关掉时 `[6,6,6,7]`（CV **16%**）——**开着反而把方差搞大了**。原因：第 2 轮跑到 60 步
    **仍在发现新页面**（无新页步数=2），说明轮 1/3 的"连续 8 步无新页"是**误触发**。
    ⇒ "连续无新页"不是"探完了"的可靠信号。
    """
    from insta_engine.explore import MAX_IDLE_STEPS, STOP_EXHAUSTED

    # 默认关：即使本夹具很快就会"连续无新页"，也不该提前停
    monkeypatch.delenv("MAT_STOP_EXHAUSTED", raising=False)
    off = explore(FakeDriver(_Counter(), "none"), "com.x", minutes=0.05, max_steps=12,
                  artifacts_dir=tmp_path / "off", max_depth=3, sleep_scale=0.01)
    assert off["stop_on_exhausted"] is False
    # 默认关 ⇒ **不该**因"连续无新页"而结束（本夹具确实会卡住，所以结束原因是 stuck，
    # 那是**另一条独立的兜底** MAX_CONSECUTIVE_BACKOFFS 在起作用——见下一条测试）
    assert off["stop_reason"] != STOP_EXHAUSTED, (
        f"默认关时不该报 exhausted，实际 {off['stop_reason']}")

    # 显式开启才提前停
    monkeypatch.setenv("MAT_STOP_EXHAUSTED", "1")
    on = explore(FakeDriver(_Counter(), "none"), "com.x", minutes=0.05, max_steps=60,
                 artifacts_dir=tmp_path / "on", max_depth=3, sleep_scale=0.01)
    assert on["stop_on_exhausted"] is True
    assert on["stop_reason"] == STOP_EXHAUSTED, (
        f"开启后探完应报 exhausted，实际 {on['stop_reason']}（步数 {on['steps']}）")
    assert on["steps"] < 60, f"开启后应在预算耗尽前停下，实际 {on['steps']} 步"
    # ⚠️ 这里用 minutes=0.05（3 秒）而不是 5.0：回退循环**不推进 `step`**，
    # 所以 `max_steps` 到不了、会一直转到 deadline——测试里用大 minutes 会挂 5 分钟。
    # 这个洞本身是真问题，见 MAX_CONSECUTIVE_BACKOFFS。
    assert on["idle_steps"] >= MAX_IDLE_STEPS
    assert "exhausted" in {f["type"] for f in on["findings"]}, "必须留下说明为什么停"


def test_budget_stop_reason_is_recorded(tmp_path):
    """预算真的耗尽时也要如实标成 `budget`——不能一律说"探完了"。

    这是"两种结束原因必须可区分"的保证：`exhausted` = 可以停手；`budget` = 该加预算。
    """
    from insta_engine.explore import STOP_BUDGET

    # 预算极小（0.01 分钟）+ 步数上限很小 ⇒ 来不及探完就停
    r = explore(FakeDriver(_Counter(), "partial"), "com.x", minutes=0.01, max_steps=1,
                artifacts_dir=tmp_path / "bg", max_depth=3, sleep_scale=0.01)
    assert r["stop_reason"] == STOP_BUDGET, (
        f"预算耗尽时应报 budget，实际 {r['stop_reason']}")


def test_backoff_spin_terminates_instead_of_burning_deadline(tmp_path):
    """**回退不推进 `step`** ⇒ 只靠 `max_steps` 挡不住空转，会一路转到时间预算耗尽。

    实测踩到：一个用了 `minutes=5.0` 的测试**挂了 5 分钟**才被外部杀掉；
    真机上的表现是"跑了 5 分钟只走 27 步"。
    ⇒ 必须有 `MAX_CONSECUTIVE_BACKOFFS` 这个**独立的**兜底（与 `MAX_LEFT_STREAK` 管的不是一件事：
    那个管"被系统挡住/离场"，这个管"页面内无路可走"）。

    本夹具：A 的候选点完后无路可走 ⇒ 应很快以 `stuck` 结束，而不是耗满 `minutes=5.0`。
    """
    import time

    from insta_engine.explore import STOP_STUCK

    t0 = time.monotonic()
    r = explore(FakeDriver(_Counter(), "none"), "com.x", minutes=5.0, max_steps=60,
                artifacts_dir=tmp_path / "stuck", max_depth=3, sleep_scale=0.01)
    elapsed = time.monotonic() - t0

    assert r["stop_reason"] == STOP_STUCK, (
        f"无路可走时应报 stuck，实际 {r['stop_reason']}（步数 {r['steps']}）")
    assert elapsed < 30, f"应在预算耗尽前就停，实际耗时 {elapsed:.1f}s（预算 300s）"
    assert "stuck" in {f["type"] for f in r["findings"]}, "必须留下说明为什么停"


# -- T19 findings 权重：高风险页回探 ----------------------------------------------

def test_pick_prefers_edge_to_risky_page():
    """通向历史高风险页（findings>0）的入口应被优先——把"发现过问题的地方"变成回探重点。

    对照组证明加分真的改变了选择：同一图谱去掉 findings 权重后，两候选同分、轮转选第一个。
    """
    from insta_engine.explore import _pick

    cands = [{"label": "普通旧页", "bounds": "[0,0][10,10]"},
             {"label": "回崩溃页", "bounds": "[0,20][10,30]"}]
    edges = {"cur": [("普通旧页", "plain"), ("回崩溃页", "crashy")]}
    hits = {"plain": 5, "crashy": 5}

    assert _pick(cands, {"cur"}, 0, edges, hits)["label"] == "普通旧页", \
        "夹具前提：无 findings 权重时两候选同分，轮转在 step=0 选第一个"
    picked = _pick(cands, {"cur"}, 0, edges, hits, {"crashy": 2})
    assert picked["label"] == "回崩溃页", f"高风险页入口应被优先，实际 {picked['label']!r}"


def test_merge_pages_keeps_findings_union():
    """findings 跨轮只增不减：崩溃/白屏计数是高风险页的依据，merge 时按类型累加。"""
    from insta_engine.page_index import merge_pages, new_index

    idx = new_index("com.t")
    merge_pages(idx, [{"page_key": "p1", "findings": {"app-crash": 1}}], source="explore")
    merge_pages(idx, [{"page_key": "p1", "findings": {"app-crash": 1, "white-screen": 1}}],
                source="explore")
    page = next(p for p in idx["pages"] if p["page_key"] == "p1")
    assert page["findings"] == {"app-crash": 2, "white-screen": 1}, f"实际 {page.get('findings')}"


def test_explore_bumps_page_findings_and_report(tmp_path):
    """端到端接线：白屏 finding 落到页面记录（随图谱持久化），报告 risky_pages 收录。"""
    import io as _io

    from PIL import Image

    counter = _Counter()
    driver = FakeDriver(counter, "none")
    origin = Image.new("L", (8, 8), 255)  # 全白截图 → white-screen finding
    buf = _io.BytesIO()
    origin.save(buf, format="PNG")

    def white_shot(path):
        Path(path).write_bytes(buf.getvalue())

    driver.screenshot = white_shot
    r = explore(driver, "com.x", minutes=0.05, max_steps=3, artifacts_dir=tmp_path)
    assert any(f["type"] == "white-screen" for f in r["findings"]), "白屏应被记为 finding"
    assert r["risky_pages"], "报告应收录高风险页"
    page_with_finding = [p for p in r["pages"] if p.get("findings")]
    assert page_with_finding, "finding 应落进页面记录（供下轮 _pick 加权与图谱持久化）"
    assert page_with_finding[0]["findings"].get("white-screen") == 1


# -- 退化 res-id（T71） --------------------------------------------------------

def test_degenerate_res_id_is_recognised():
    """**占位 res-id 的判据是"名字以数字开头"**，不是硬编码某个字符串。

    Android 资源名必须是合法 Java 标识符（**不能以数字开头**）⇒ 以数字开头的一律是
    aapt2 的占位名。实测 4 个 App 的图谱里只有 `0_resource_name_obfuscated` 一个。
    """
    from insta_engine.page_index import is_degenerate_res_id

    assert is_degenerate_res_id("com.xingin.xhs:id/0_resource_name_obfuscated")
    assert not is_degenerate_res_id("com.xingin.xhs:id/search_box")
    assert not is_degenerate_res_id("com.x:id/_0_ok")      # 下划线开头是合法资源名
    assert not is_degenerate_res_id("")                     # 空的不算


def test_degenerate_res_id_falls_back_to_text():
    """**核心**：带占位 rid 的控件必须**回落到文案**，否则它的差异会被盖掉。

    实测（T71）：`identities_from_controls` 里 rid 优先且无条件加入 ⇒
    某 App 121 个 GlobalSearch 页里 **41 页信号集完全相同**（都只剩占位 rid）。
    回落之后，同一批页的信号集**各不相同**（搜索词进了指纹）。
    """
    from insta_engine.page_index import identities_from_controls

    a = identities_from_controls([{"res-id": "com.x:id/0_resource_name_obfuscated", "text": "苹果"}])
    b = identities_from_controls([{"res-id": "com.x:id/0_resource_name_obfuscated", "text": "香蕉"}])
    assert a != b, "占位 rid 相同但文案不同 ⇒ 信号集必须不同（否则指纹被抹平）"
    assert a == {"text:苹果"}, f"应回落到文案，实际 {a}"

    # **反例**：正常 res-id 仍然优先（不能因为这次改动把 rid 丢了）
    c = identities_from_controls([{"res-id": "com.x:id/search_box", "text": "搜索"}])
    assert c == {"rid:com.x:id/search_box"}, f"正常 rid 应优先，实际 {c}"


# -- 渐进加载归并（老覆盖率）+ 选点重放语义加权（2026-10-17，tx-news 重探实测）----------------------


def test_prev_coverage_direction_semantics():
    """老覆盖率的方向语义：渐进加载（老⊆新）≈1；内容轮换/真新页低。"""
    from insta_engine.explore import _prev_coverage
    old = frozenset({"a", "b", "c", "d"})
    grown = frozenset({"a", "b", "c", "d", "e", "f"})       # 渐进加载：老全保留
    churned = frozenset({"e", "f", "g", "h"})               # 内容轮换：老全没了
    assert _prev_coverage(old, grown) == 1.0
    assert _prev_coverage(old, churned) == 0.0
    assert _prev_coverage(frozenset(), grown) == 0.0        # 空老帧不表态


def test_prev_coverage_beats_containment_for_progressive_loading():
    """containment 被新增信号稀释（20→50 信号 0.4<0.8），老覆盖率 1.0 ——
    这正是 tx-news 重探 62/75 新页无链的主因（渐进帧被判新页，pending_nav 已清）。"""
    from insta_engine.explore import _containment, _prev_coverage
    old = frozenset({f"s{i}" for i in range(20)})
    new = old | {f"n{i}" for i in range(30)}
    assert _containment(old, new) < 0.8                     # 旧判据漏归并
    assert _prev_coverage(old, new) >= 0.9                  # 新判据归并


def test_pick_replay_semantics_weighting():
    """选点重放语义加权：非PUA文案+3 / rid+2 / 两者+5 / PUA且无rid=0。"""
    from insta_engine.explore import _pick
    cands = [
        {"label": "\ue90f", "rid": "", "bounds": "[0,0][10,10]"},     # PUA 无 rid：0
        {"label": "设置", "rid": "", "bounds": "[0,10][10,20]"},      # 文案：3
        {"label": "", "rid": "a/btn_x", "bounds": "[0,20][10,30]"},   # rid：2
        {"label": "我的", "rid": "a/btn_me", "bounds": "[0,30][10,40]"},  # 5
    ]
    assert _pick(cands, set(), 0, {}, {})["label"] == "我的"
    # 去掉最高分，轮转范围只含可重放候选，PUA 垫底
    rest = [c for c in cands if c["label"] != "我的"]
    picks = {_pick(rest, set(), i, {}, {})["bounds"] for i in range(8)}
    assert "[0,0][10,10]" not in picks, "PUA 且无 rid 的控件不应被选中"


def test_pick_pua_with_rid_still_replayable():
    """iconfont 控件带 rid 仍可重放（rid 通道）——不能一刀切降到底。"""
    from insta_engine.explore import _pick
    pua_rid = {"label": "\ue9b9", "rid": "a/btn_listener", "bounds": "[0,0][10,10]"}
    no_sem = {"label": "", "rid": "", "bounds": "[0,20][10,30]"}
    assert _pick([pua_rid, no_sem], set(), 0, {}, {})["bounds"] == "[0,0][10,10]"
