"""用例定位审计（`case_audit`）的规则测试：清单类（R18）/ 弹窗类（R19）/ 跨页类（R20）。

R20 是 2026-10-21 新增：把"定位是否在图谱里"的粒度从**全图谱**改成**本步所在页**。
"""

from __future__ import annotations

from insta_engine.dsl import Action, LocatorStrategy, TestCase
from insta_engine.page_index import identities_from_controls

PKG = "com.x"


def _page(key: str, **kw) -> dict:
    base = {"page_key": key, "depth": 1, "parent_key": None, "parent_action": None,
            "activity": f"{PKG}/.A", "identities": [], "controls": [],
            "alt_entries": [], "hits": 1}
    base.update(kw)
    if not base["identities"] and base["controls"]:
        base["identities"] = sorted(identities_from_controls(base["controls"]))
    return base

# -- 2026-10-21 补：R20 跨页定位（粒度从"全图谱"改成"本步所在页"）----------------------------


def _r20_case(steps: list[dict], name: str = "跨页") -> TestCase:
    return TestCase(caseId="case_000001", name=name, target={"package": PKG}, steps=steps)


def _step(sid: str, action, value: str = "", strategy=LocatorStrategy.TEXT,
          assertions: list | None = None) -> dict:
    data: dict = {"id": sid, "action": action}
    if value:
        data["target"] = {"strategy": strategy, "value": value}
    if assertions:
        data["assertions"] = assertions
    return data


def _r20_index(tmp_path, pages: list[dict]):
    from insta_engine.page_index import save_index

    save_index(tmp_path / f"{PKG}.json", {"package": PKG, "generated_at": "t",
                                         "sources": ["explore"], "pages": pages, "edges": []})
    return tmp_path


def test_r20_flags_locator_that_only_exists_on_another_page(tmp_path):
    """定位在**全图谱有**、但本步所在页没有 ⇒ 报 R20（R18 放过了这种）。

    真机背景（2026-10-21 某生活服务 App）：`txt_address_info` 在全图谱 6 个「外卖」碎片上都存在，
    但用例点「外卖」落到的是另一个碎片，运行时定位失败——R18 的"全图谱"粒度太宽。
    """
    from insta_engine.case_audit import audit_case

    pages = [
        _page("root", depth=0, controls=[{"text": "外卖", "clickable": True}]),
        _page("takeout", depth=1, parent_key="root", parent_action="外卖",
              controls=[{"text": "搜索", "clickable": True}]),
        _page("addr", depth=1, parent_key="root", parent_action="我的",
              controls=[{"res-id": "com.x:id/txt_address_info", "clickable": True}]),
    ]
    idx = _r20_index(tmp_path, pages)
    case = _r20_case([
        _step("s1", Action.LAUNCH),
        _step("s2", Action.TAP, "外卖"),
        _step("s3", Action.WAIT, "com.x:id/txt_address_info", LocatorStrategy.RES_ID),
    ])
    issues = audit_case(case, idx)
    r20 = [i for i in issues if "[R20]" in i]
    assert len(r20) == 1 and "s3" in r20[0], issues
    assert not any("[R18]" in i for i in issues), "全图谱有 ⇒ 不该报 R18"


def test_r20_silent_when_locator_is_on_expected_page(tmp_path):
    from insta_engine.case_audit import audit_case

    pages = [
        _page("root", depth=0, controls=[{"text": "外卖", "clickable": True}]),
        _page("takeout", depth=1, parent_key="root", parent_action="外卖",
              controls=[{"res-id": "com.x:id/txt_address_info", "clickable": True}]),
    ]
    idx = _r20_index(tmp_path, pages)
    case = _r20_case([
        _step("s1", Action.LAUNCH),
        _step("s2", Action.TAP, "外卖"),
        _step("s3", Action.WAIT, "com.x:id/txt_address_info", LocatorStrategy.RES_ID),
    ])
    assert not [i for i in audit_case(case, idx) if "[R20]" in i]


def test_r20_silent_when_page_cannot_be_inferred(tmp_path):
    """走链断了（找不到边）⇒ 页面未知 ⇒ 不判（宁可漏报，不可误报）。"""
    from insta_engine.case_audit import audit_case

    pages = [
        _page("root", depth=0, controls=[{"text": "外卖", "clickable": True}]),
        _page("addr", depth=1, parent_key="root", parent_action="我的",
              controls=[{"res-id": "com.x:id/txt_address_info", "clickable": True}]),
    ]
    idx = _r20_index(tmp_path, pages)
    case = _r20_case([
        _step("s1", Action.LAUNCH),
        _step("s2", Action.TAP, "首页"),          # 图谱里没有这条边 ⇒ 后续页面未知
        _step("s3", Action.WAIT, "com.x:id/txt_address_info", LocatorStrategy.RES_ID),
    ])
    assert not [i for i in audit_case(case, idx) if "[R20]" in i]


def test_r20_follows_res_id_edge(tmp_path):
    """点击按 res-id 时也要能沿图谱走链（icon-only 控件没有文案）。

    实测背景（2026-10-21 某生活服务 App）：图谱 80 条父链只有文案（`parent_rid` 为空），而生成用例
    大量用 res-id 点击 ⇒ 走链在第一步就断，R20 全程弃权。explore 从 2026-10 起记
    `parent_rid`，走链必须认它，否则新图谱也白记。
    """
    from insta_engine.case_audit import audit_case

    pages = [
        _page("root", depth=0, controls=[{"text": "外卖", "res-id": "com.x:id/tab_takeout",
                                          "clickable": True}]),
        _page("takeout", depth=1, parent_key="root", parent_rid="com.x:id/tab_takeout",
              controls=[{"text": "搜索", "clickable": True}]),
        _page("addr", depth=1, parent_key="root", parent_action="我的",
              controls=[{"res-id": "com.x:id/txt_address_info", "clickable": True}]),
    ]
    idx = _r20_index(tmp_path, pages)
    case = _r20_case([
        _step("s1", Action.LAUNCH),
        _step("s2", Action.TAP, "com.x:id/tab_takeout", LocatorStrategy.RES_ID),
        _step("s3", Action.WAIT, "com.x:id/txt_address_info", LocatorStrategy.RES_ID),
    ])
    issues = [i for i in audit_case(case, idx) if "[R20]" in i]
    assert len(issues) == 1 and "s3" in issues[0], issues


# -- 2026-10-21 补：R21 文案定位歧义 ---------------------------------------------------------


def _tap_case(value: str, strategy=LocatorStrategy.TEXT) -> TestCase:
    return _r20_case([_step("s1", Action.LAUNCH), _step("s2", Action.TAP, value, strategy)])


def test_r21_flags_text_and_desc_same_name(tmp_path):
    """同页同时有 `text=外卖`（内容标签）与 `desc=外卖`（图标入口）⇒ 报 R21。

    真机背景（2026-10-21 某生活服务 App）：入口是 `desc="外卖"` 的图标 View，商品卡上还有 `text="外卖"`
    的角标。`locator._try` 的 TEXT 链是 **text 精确 → desc 精确**，于是先命中角标 ⇒
    点开搜索页，用例在错误页面上继续，引擎当时没有任何提示。
    """
    from insta_engine.case_audit import audit_case

    pages = [_page("root", depth=0, controls=[
        {"text": "外卖", "clickable": True},                  # 商品卡角标
        {"desc": "外卖", "clickable": True},                  # 真正的图标入口
        {"text": "我的", "clickable": True},
    ])]
    issues = [i for i in audit_case(_tap_case("外卖"), _r20_index(tmp_path, pages)) if "[R21]" in i]
    assert len(issues) == 1 and "content-desc" in issues[0], issues


def test_r21_flags_multiple_same_text(tmp_path):
    from insta_engine.case_audit import audit_case

    pages = [_page("root", depth=0, controls=[
        {"text": "外卖", "clickable": True}, {"text": "外卖", "clickable": True}])]
    issues = [i for i in audit_case(_tap_case("外卖"), _r20_index(tmp_path, pages)) if "[R21]" in i]
    assert len(issues) == 1 and "同名控件" in issues[0], issues


def test_r21_flags_desc_only_entry(tmp_path):
    from insta_engine.case_audit import audit_case

    pages = [_page("root", depth=0, controls=[{"desc": "外卖", "clickable": True}])]
    issues = [i for i in audit_case(_tap_case("外卖"), _r20_index(tmp_path, pages)) if "[R21]" in i]
    assert len(issues) == 1 and "content-desc" in issues[0], issues


def test_r21_silent_for_unique_text(tmp_path):
    from insta_engine.case_audit import audit_case

    pages = [_page("root", depth=0, controls=[{"text": "搜索", "clickable": True}])]
    assert not [i for i in audit_case(_tap_case("搜索"), _r20_index(tmp_path, pages))
                if "[R21]" in i]


def _rid_index_case(rid: str, index: int | None) -> TestCase:
    target: dict = {"strategy": "res-id", "value": rid}
    if index is not None:
        target["index"] = index
    return TestCase.model_validate({
        "caseId": "c", "name": "切频道", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap", "target": target}]})


def test_r21_silent_for_rid_with_explicit_index(tmp_path):
    """带显式 `index` 的 res-id **不是歧义**——它恰恰是唯一能精确点到某个 tab 的写法。

    来源（2026-10-28）：`gen-183555-3` 按候选集的 `index→文案` 映射写了
    `res-id=channel_bar_item_container, index=5`（5=国际，完全正确），R21 仍报
    "建议改用 text" ⇒ 又把**唯一可行写法**判成缺陷（与 R13 同族）。
    """
    from insta_engine.case_audit import audit_case

    idx = _r20_index(tmp_path, _shared_rid_pages())
    issues = [i for i in audit_case(_rid_index_case(f"{PKG}:id/channelbar_text", 3), idx)
              if "[R21]" in i]
    assert issues == [], issues


def test_r21_flags_out_of_range_index(tmp_path):
    """`index` 越界要报：7 个实例里写 99 ⇒ 运行期定位失败或退化成第一个。"""
    from insta_engine.case_audit import audit_case

    idx = _r20_index(tmp_path, _shared_rid_pages())
    issues = [i for i in audit_case(_rid_index_case(f"{PKG}:id/channelbar_text", 99), idx)
              if "[R21]" in i]
    assert len(issues) == 1 and "越界" in issues[0], issues


# -- 2026-10-21 补：共用 res-id 的确定性改正 + 走链忽略启动噪声 -------------------------------


def _shared_rid_pages() -> list[dict]:
    """7 个频道 tab 共用同一 res-id（真机形态）。"""
    tabs = ["7x24", "要闻", "关注", "视频", "天津", "国际", "娱乐"]
    return [_page("home", depth=0, controls=[
        {"text": t, "res-id": f"{PKG}:id/channelbar_text", "clickable": False} for t in tabs
    ])]


def _channel_case(value: str, intent: str, params: dict | None = None) -> dict:
    return {"caseId": "c", "name": "频道", "target": {"package": PKG},
            "steps": [{"id": "s1", "action": "launch"},
                      {"id": "s2", "action": "tap", "intent": intent,
                       "params": params or {},
                       "target": {"strategy": "res-id", "value": value}}]}


def test_sanitize_ambiguous_rids_rewrites_by_intent(tmp_path):
    """共用 res-id + 意图只指向其中一个 ⇒ 自动改成那条文案。

    真机背景：7 个频道 tab 共用 `channelbar_text`，用例写 res-id 就永远点第一个 tab，
    而每一步都报成功；用例自己的 `intent`/`params.text` 已经写明了要哪个。
    """
    from insta_engine.case_audit import sanitize_ambiguous_rids

    idx = _r20_index(tmp_path, _shared_rid_pages())
    raw = _channel_case(f"{PKG}:id/channelbar_text", "点击首页的要闻频道栏", {"text": "要闻"})
    issues = sanitize_ambiguous_rids(raw, idx)
    assert raw["steps"][1]["target"] == {"strategy": "text", "value": "要闻"}, raw["steps"][1]
    assert len(issues) == 1 and "[R21]" in issues[0], issues


def test_sanitize_ambiguous_rids_leaves_ambiguous_intent_alone(tmp_path):
    """意图不唯一（或指向的文案不在该页）⇒ **不改**，只让 R21 去报。"""
    from insta_engine.case_audit import sanitize_ambiguous_rids

    idx = _r20_index(tmp_path, _shared_rid_pages())
    raw = _channel_case(f"{PKG}:id/channelbar_text", "点击频道栏", {})
    assert sanitize_ambiguous_rids(raw, idx) == []
    assert raw["steps"][1]["target"]["strategy"] == "res-id"


def test_sanitize_ambiguous_rids_skips_explicit_index(tmp_path):
    """作者已用 `index` 指明「第几个」⇒ 共用 rid 是**有意为之**，不再按意图猜文案。

    为什么必须放行：R21 的本意是「共用 rid 会永远点第一个」，而 `index` 恰好解决了这件事。
    此时按意图改写成某条文案会把「位置型意图」变回「内容型」——对信息流卡片是更脆的方向
    （内容每天都变），与 R21 要解决的问题相反。
    """
    from insta_engine.case_audit import sanitize_ambiguous_rids

    idx = _r20_index(tmp_path, _shared_rid_pages())
    raw = _channel_case(f"{PKG}:id/channelbar_text", "点击首页的要闻频道栏", {"text": "要闻"})
    raw["steps"][1]["target"]["index"] = 1
    assert sanitize_ambiguous_rids(raw, idx) == []
    assert raw["steps"][1]["target"] == {
        "strategy": "res-id", "value": f"{PKG}:id/channelbar_text", "index": 1}



def test_walk_treats_noise_tap_as_page_unchanged(tmp_path):
    """`tap 跳过` 不换页 ⇒ 后续步骤仍能判在某页上（否则 R20/R21 全程弃权）。"""
    from insta_engine.case_audit import page_guard_coverage

    pages = [_page("home", depth=0, controls=[{"text": "要闻", "clickable": True}])]
    idx = _r20_index(tmp_path, pages)
    case = TestCase.model_validate({
        "caseId": "c", "name": "n", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "跳过"}},
                  {"id": "s3", "action": "tap", "target": {"strategy": "text", "value": "要闻"}}]})
    judged, total = page_guard_coverage(case, idx, PKG)
    assert (judged, total) == (1, 1), f"跳过不该占判定名额，实际 {judged}/{total}"


# -- 2026-10-22 补：R24 断言在动作之前就成立（判不出页面变没变）--------------------------
#
# 来源：A1 假绿复核。3 条假绿的断言都是 `exists 要闻`，而「要闻」**在点击前就在首页频道栏里**——
# 切没切它都在。第一版判据用的是"文案在 ≥10% 页面出现就报"，在某内容社区 App上误报（`搜索,`
# 出现在 62 页，但那 62 页全是搜索页碎片，它恰恰能证明到了搜索页）。
# ⇒ 判据改成**状态转移**：动作之前成不成立。


def _nav_index(tmp_path, before_controls: list[dict], after_controls: list[dict]):
    """首页（有 X）+ 目标页（有 Y），并建一条 home -tap 搜索-> target 的边。"""
    home = _page("home", depth=0, controls=before_controls)
    target = _page("target", depth=1, parent_key="home", parent_action="搜索",
                   controls=after_controls)
    from insta_engine.page_index import save_index

    save_index(tmp_path / f"{PKG}.json",
               {"package": PKG, "generated_at": "t", "sources": ["explore"],
                "pages": [home, target],
                "edges": [{"from": "home", "to": "target", "action": "搜索",
                           "action_type": "tap"}]})
    return tmp_path


def _two_step_case(value: str, assertion: dict, tap: str = "搜索") -> TestCase:
    return TestCase.model_validate({
        "caseId": "c", "name": "n", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap",
                   "target": {"strategy": "text", "value": tap}},
                  {"id": "s3", "action": "wait", "assertions": [assertion]}]})


def test_r24_flags_assertion_that_already_held_before_the_action(tmp_path):
    """`tap 搜索` 后断言 `exists 搜索`，而「搜索」在**点击前那页**就有 ⇒ 判不出页面变没变。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _nav_index(tmp_path,
                     [{"text": "搜索", "clickable": True}, {"text": "推荐", "clickable": True}],
                     [{"text": "历史记录", "clickable": False}])
    case = _two_step_case("搜索", {"type": "exists",
                                   "target": {"strategy": "text", "value": "搜索"}})
    warns = nondiscriminating_assertions(case, idx, PKG)
    assert any("[R24]" in w and "动作之前" in w for w in warns), warns


def test_r24_is_silent_for_a_target_page_only_control(tmp_path):
    """反向：断言的是**目标页独有**的控件 ⇒ 能判出页面变了，不许报。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _nav_index(tmp_path, [{"text": "搜索", "clickable": True}],
                     [{"text": "历史记录", "clickable": False}])
    case = _two_step_case("搜索", {"type": "exists",
                                   "target": {"strategy": "text", "value": "历史记录"}})
    assert nondiscriminating_assertions(case, idx, PKG) == []


def test_r24_is_silent_for_state_assertions(tmp_path):
    """**状态断言不走这条**：元素一直在，但选中态会变 ⇒ 有区分度。

    实测误伤过一次：把状态断言也按"动作前已成立"报，会报掉语料里最好的一条
    （`selected 我的`，底部 tab 唯一、状态会变）。
    """
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _nav_index(tmp_path, [{"text": "我的", "clickable": True}],
                     [{"text": "我的", "clickable": True}])
    case = _two_step_case("我的", {"type": "selected",
                                   "target": {"strategy": "text", "value": "我的"}})
    assert nondiscriminating_assertions(case, idx, PKG) == []


def test_r24_is_silent_without_navigation(tmp_path):
    """没有导航动作（元素清单类用例）⇒ 不报：它的意图就是"这些常驻元素在"。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _nav_index(tmp_path, [{"text": "搜索", "clickable": True}], [])
    case = TestCase.model_validate({
        "caseId": "c", "name": "断言：首页关键元素", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "wait",
                   "assertions": [{"type": "exists",
                                   "target": {"strategy": "text", "value": "搜索"}}]}]})
    assert nondiscriminating_assertions(case, idx, PKG) == []


def test_r24_flags_state_assertion_on_a_container_res_id(tmp_path):
    """**容器 rid**：一个 rid 覆盖多个不同文案 ⇒ 断它的状态只反映第一个。

    来源（2026-10-22 生成侧 A/B）：模型很听话地把 `exists text=要闻` 换成了
    `selected res-id=channelbar_text` —— **类型变了，判据没变**：这个 rid 对应 7 个 tab，
    `selected` 只能反映第一个（7x24，首页本来就选中）⇒ 恒为真。
    """
    from insta_engine.case_audit import nondiscriminating_assertions

    controls = [{"res-id": f"{PKG}:id/channelbar_text", "text": t, "clickable": True}
                for t in ("7x24", "要闻", "关注", "视频")]
    idx = _nav_index(tmp_path, controls, controls)
    case = TestCase.model_validate({
        "caseId": "c", "name": "切换频道", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap",
                   "target": {"strategy": "res-id", "value": f"{PKG}:id/channelbar_text"}},
                  {"id": "s3", "action": "wait",
                   "assertions": [{"type": "selected",
                                   "target": {"strategy": "res-id",
                                              "value": f"{PKG}:id/channelbar_text"}}]}]})
    warns = nondiscriminating_assertions(case, idx, PKG)
    assert any("[R24]" in w and "容器" in w for w in warns), warns


def test_r24_abstains_without_a_page_index(tmp_path):
    """没有该 App 的索引 ⇒ 弃权（不猜）。"没报 R24" 不等于"断言没问题"。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    case = _two_step_case("搜索", {"type": "exists",
                                   "target": {"strategy": "text", "value": "搜索"}})
    assert nondiscriminating_assertions(case, tmp_path, PKG) == []


# -- 2026-10-28 补：R26 走链弃权后的兜底（R12）------------------------------------------
#
# 来源：R24 的**覆盖边界**。落点页不在图谱里时走链断在动作那一步，`pre_nav` 变 None
# ⇒ R24 全程弃权、一条不报。实测两种形态：
#   · `exists res-id=c_list_view_container`（tx 图谱 5/24 页，**首页就有**）在 `tap 搜索`
#     之后不再有区分度，但搜索结果页不在图谱 ⇒ 静默；
#   · `exists imgLogo` 只在「人民网主页」1 页出现，「我的」页不在图谱 24 页里 ⇒ 也静默。
# 兜底判据 = 「目标在**本用例任一已知前序页**上就有」（不引入新阈值，语义与 R24 第一条一致）。
# ⚠️ **否掉**「按 rid 出现页数兜底」：反例 `action_bar_comment` 13/24 页**全是详情页**，
#    断它仍能证明"进了详情页"（与某内容社区 App `搜索,` 62 页误报同构）。


def _broken_walk_index(tmp_path, home_controls: list[dict]):
    """只有首页（`tap 搜索` 在图谱里**没有边** ⇒ 走链从 s2 起弃权）。"""
    from insta_engine.page_index import save_index

    save_index(tmp_path / f"{PKG}.json",
               {"package": PKG, "generated_at": "t", "sources": ["explore"],
                "pages": [_page("home", depth=0, controls=home_controls)],
                "edges": []})
    return tmp_path


def _search_case(assertion: dict) -> TestCase:
    """launch → tap 搜索 → input → tap 搜索按钮 → wait 断言（模拟真实搜索类用例）。"""
    return TestCase.model_validate({
        "caseId": "c", "name": "搜索后校验", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap",
                   "target": {"strategy": "text", "value": "搜索"}},
                  {"id": "s3", "action": "input",
                   "target": {"strategy": "res-id", "value": f"{PKG}:id/search_box"},
                   "value": "新闻"},
                  {"id": "s4", "action": "tap",
                   "target": {"strategy": "text", "value": "搜索"}},
                  {"id": "s5", "action": "wait", "assertions": [assertion]}]})


def test_r26_flags_assertion_that_held_on_a_known_earlier_page(tmp_path):
    """走链弃权后，断言目标在**首页**（已知前序页）就有 ⇒ 证明不了到了搜索结果页。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    rid = f"{PKG}:id/c_list_view_container"
    idx = _broken_walk_index(tmp_path, [
        {"text": "搜索", "clickable": True},
        {"res-id": rid, "clickable": True},
    ])
    case = _search_case({"type": "exists",
                         "target": {"strategy": "res-id", "value": rid}})
    warns = nondiscriminating_assertions(case, idx, PKG)
    assert any("[R26]" in w and "已知前序页" in w for w in warns), warns


def test_r26_is_silent_for_a_control_absent_from_all_earlier_pages(tmp_path):
    """反向：目标在前序页里**没有** ⇒ 它可能就是落点页独有的，不许报（宁可漏报）。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _broken_walk_index(tmp_path, [{"text": "搜索", "clickable": True}])
    case = _search_case({"type": "exists",
                         "target": {"strategy": "text", "value": "历史记录"}})
    assert nondiscriminating_assertions(case, idx, PKG) == []


def test_r26_does_not_fire_without_a_broken_walk(tmp_path):
    """**没断过链就不许兜底**：否则 launch 之后那一步会把"第一页自己的断言"误报成
    "动作前就成立"（元素清单类用例全中）。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _broken_walk_index(tmp_path, [{"text": "搜索", "clickable": True}])
    case = TestCase.model_validate({
        "caseId": "c", "name": "首页关键元素", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "wait",
                   "assertions": [{"type": "exists",
                                   "target": {"strategy": "text", "value": "搜索"}}]}]})
    assert nondiscriminating_assertions(case, idx, PKG) == []


def test_r26_is_silent_for_state_assertions(tmp_path):
    """状态断言与 R24 同一条豁免（选中态会变 ⇒ 有区分度）。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _broken_walk_index(tmp_path, [{"text": "我的", "clickable": True}])
    case = _search_case({"type": "selected",
                         "target": {"strategy": "text", "value": "我的"}})
    assert nondiscriminating_assertions(case, idx, PKG) == []


# -- 2026-10-26 补：R18 不得用「展示用截断」的控件清单做存在性判定 -----------------------------


def test_r18_sees_controls_beyond_the_display_cap(tmp_path):
    """R18 是**存在性判定**，不能只看前 300 条。

    `controls_list_for` 的默认 `max_controls=300` 是给配置台**下拉候选**的展示上限。
    拿它做判定，等于把没截到的页面全判成"编造"。

    真机背景（2026-10-26，某资讯类 App）：图谱 24 页 / 1499 个控件，前 300 条**只覆盖 1 个
    activity**（首页）⇒ 详情页的 `news_detail_title`、`action_bar_comment`、
    `title_bar_share_btn` 全被 R18 误报成"不在控件清单"，而这几个 rid 在真机上确实存在
    （同批用例 3/3 通过，且真机 dump 里能直接看到）。
    """
    from insta_engine.case_audit import audit_case

    home_controls = [{"res-id": f"{PKG}:id/home_{i}", "clickable": True} for i in range(320)]
    pages = [
        _page("root", depth=0, controls=home_controls),
        _page("detail", depth=1, parent_key="root", parent_action="home_0",
              controls=[{"res-id": f"{PKG}:id/detail_title"}]),
    ]
    idx = _r20_index(tmp_path, pages)
    case = _r20_case([
        _step("s1", Action.LAUNCH),
        _step("s2", Action.TAP, f"{PKG}:id/home_0", LocatorStrategy.RES_ID),
        _step("s3", Action.WAIT, f"{PKG}:id/detail_title", LocatorStrategy.RES_ID),
    ])
    issues = audit_case(case, idx)
    assert not any("[R18]" in i for i in issues), (
        "详情页的控件在 300 条之外，但它在图谱里 ⇒ 不该报 R18；"
        f"实际报了：{[i for i in issues if '[R18]' in i]}")


def test_controls_list_for_none_means_unlimited(tmp_path):
    """`max_controls=None` 必须表示不截断（展示与判定要能分开）。"""
    from insta_engine.page_index import controls_list_for, save_index

    save_index(tmp_path / f"{PKG}.json", {
        "package": PKG, "generated_at": "t", "sources": ["explore"], "edges": [],
        "pages": [_page("root", depth=0,
                        controls=[{"res-id": f"{PKG}:id/c{i}"} for i in range(400)])],
    })
    assert len(controls_list_for(PKG, tmp_path)) == 300, "默认仍是 300（下拉候选的展示上限）"
    assert len(controls_list_for(PKG, tmp_path, max_controls=None)) == 400


# -- 2026-10-28 补：R27「状态断言的目标在动作前就已选中」 ------------------------------------
#
# 判据依赖图谱里的 `selected`（清单里的 ★）。语料里的典型假绿：`tap 要闻` + `selected 要闻`，
# 而首页**本来就停在要闻** ⇒ 动作 no-op、断言恒真。
# ⚠️ 与 R24 分开编号：R24 的口径是"目标在动作前那页存在"（对**存在类**断言），
#    R27 的口径是"状态断言的目标**已经是选中态**"。混号会让一个计数指向两个修复方向。

_TABS = ["7x24", "要闻", "关注", "视频", "天津", "国际", "娱乐"]


def _tabs_controls(selected_idx: int | None) -> list[dict]:
    """频道栏：容器 + 文案两套 rid，与真机 tx-news 一致（同 bounds 对齐）。"""
    out: list[dict] = []
    for i, label in enumerate(_TABS):
        bounds = f"[{i * 138},221][{(i + 1) * 138},318]"
        sel = (i == selected_idx)
        out.append({"res-id": f"{PKG}:id/channel_bar_item_container", "text": "",
                    "desc": "", "clickable": True, "bounds": bounds,
                    **({"selected": True} if sel else {})})
        out.append({"res-id": f"{PKG}:id/channelbar_text", "text": label,
                    "desc": "", "clickable": False, "bounds": bounds,
                    **({"selected": True} if sel else {})})
    return out


def _tabs_index(tmp_path, selected_idx: int | None):
    """只有首页、**没有边**（tab 切换不换页 ⇒ 图谱里本来就没有自环边）。"""
    from insta_engine.page_index import save_index

    save_index(tmp_path / f"{PKG}.json",
               {"package": PKG, "generated_at": "t", "sources": ["explore"],
                "pages": [_page("home", depth=0, controls=_tabs_controls(selected_idx))],
                "edges": []})
    return tmp_path


def _tab_case(assertion: dict) -> TestCase:
    return TestCase.model_validate({
        "caseId": "c", "name": "切频道", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap",
                   "target": {"strategy": "text", "value": "要闻"}},
                  {"id": "s3", "action": "wait", "assertions": [assertion]}]})


def _codes(warns: list[str]) -> set[str]:
    return {w.split("]")[0].lstrip("[") for w in warns}


def test_r27_flags_state_assertion_on_an_already_selected_tab(tmp_path):
    """`tap 要闻` + `selected 要闻`，而首页本来就停在要闻 ⇒ 假绿。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _tabs_index(tmp_path, selected_idx=1)
    case = _tab_case({"type": "selected",
                      "target": {"strategy": "text", "value": "要闻"}})
    warns = nondiscriminating_assertions(case, idx, PKG)
    assert "R27" in _codes(warns), warns
    assert any("动作前就已经是选中态" in w for w in warns)


def test_r27_is_silent_for_a_tab_that_is_not_selected_yet(tmp_path):
    """断「国际」——它不是当前选中项 ⇒ 有区分度，不报。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _tabs_index(tmp_path, selected_idx=1)
    case = _tab_case({"type": "selected",
                      "target": {"strategy": "text", "value": "国际"}})
    assert "R27" not in _codes(nondiscriminating_assertions(case, idx, PKG))


def test_r27_respects_explicit_index_on_a_container(tmp_path):
    """容器 rid 必须按 `index` 取实例：index=5（国际）不报，index=1（要闻）报。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _tabs_index(tmp_path, selected_idx=1)
    ok = _tab_case({"type": "selected",
                    "target": {"strategy": "res-id",
                               "value": f"{PKG}:id/channel_bar_item_container", "index": 5}})
    assert "R27" not in _codes(nondiscriminating_assertions(ok, idx, PKG))

    bad = _tab_case({"type": "selected",
                     "target": {"strategy": "res-id",
                                "value": f"{PKG}:id/channel_bar_item_container", "index": 1}})
    assert "R27" in _codes(nondiscriminating_assertions(bad, idx, PKG))


def test_r27_is_silent_without_selected_data(tmp_path):
    """老图谱没采 `selected` ⇒ 判不了 ⇒ 静默（"没报"≠"没问题"）。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _tabs_index(tmp_path, selected_idx=None)
    case = _tab_case({"type": "selected",
                      "target": {"strategy": "text", "value": "要闻"}})
    assert "R27" not in _codes(nondiscriminating_assertions(case, idx, PKG))


def test_r27_is_silent_when_the_target_is_absent_from_the_page(tmp_path):
    """目标不在动作前那页（走链断了、pre_nav 也没了）⇒ 不猜。"""
    from insta_engine.case_audit import nondiscriminating_assertions

    idx = _tabs_index(tmp_path, selected_idx=1)
    case = TestCase.model_validate({
        "caseId": "c", "name": "x", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "wait",
                   "assertions": [{"type": "selected",
                                   "target": {"strategy": "text", "value": "不存在的tab"}}]}]})
    assert "R27" not in _codes(nondiscriminating_assertions(case, idx, PKG))


def test_r27_is_silent_on_a_non_root_page(tmp_path):
    """非启动落点页的 ★ 是**采集路径造成的状态**，不能当"App 默认态" ⇒ 不判。

    实测来源：新图谱里 `首页-7x24标签页` 被判成假绿，但它是真的切了 tab——碎片
    `7b664fd2a93a`（parent=根页）的快照里 7x24 已选中，因为爬虫点了它才拍到的。
    """
    from insta_engine.case_audit import nondiscriminating_assertions
    from insta_engine.page_index import save_index

    save_index(tmp_path / f"{PKG}.json",
               {"package": PKG, "generated_at": "t", "sources": ["explore"],
                "pages": [_page("home", depth=0, controls=_tabs_controls(1)),
                          _page("frag", depth=1, parent_key="home",
                                parent_action="7x24", controls=_tabs_controls(0))],
                "edges": [{"from": "home", "to": "frag", "action": "7x24"}]})
    case = TestCase.model_validate({
        "caseId": "c", "name": "7x24", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap",
                   "target": {"strategy": "text", "value": "7x24"}},
                  {"id": "s3", "action": "wait",
                   "assertions": [{"type": "selected",
                                   "target": {"strategy": "text", "value": "7x24"}}]}]})
    assert "R27" not in _codes(nondiscriminating_assertions(case, tmp_path, PKG))
