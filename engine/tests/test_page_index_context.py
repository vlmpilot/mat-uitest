"""控件清单注入形态（`page_index.controls_context`）——R13 的护栏。

## 为什么这几条要单独锁

2026-10-28 批量生成实测（11 条意图 ×3 轮）：某资讯 App 的频道栏在注入清单里
被写成 **7 行同 rid、文案各不同** 的形式——

    - res-id: .../channelbar_text  文案: 7x24
    - res-id: .../channelbar_text  文案: 要闻
    - res-id: .../channelbar_text  文案: 国际     ← 同一 rid 出现 7 行

读起来像"7 个恰好同 id 的控件"，模型据此把 `text=国际` 与 `res-id=channelbar_text` 当成
两个等价选项，写了 `text=国际`，而 R21 立刻报"3 个同名控件"。**候选集没告诉它这是共用容器。**

⇒ 同一 rid 的多个文案合并成一行，并标出"这是共用容器"。下面几条锁住"合并了""顺序对"
"别无限长""别把单文案也加噪"，以及第 ② 半的 `index→文案` 映射。

⚠️ 2026-10-28 复核时发现第 ① 半**不够**：合并行的提示原来写着"用 text=某文案 或按此顺序
用 index"——前半句在推荐 R21 判为歧义的那种写法，后半句是错的（文案行列出的是**去重**文案，
`index` 数的是**实例**，含重复快照，两个顺序对不上）。真正的解法是给可点容器的 `index→文案`。
"""
from __future__ import annotations

from insta_engine.page_index import controls_context


def _page(controls: list[dict]) -> list[dict]:
    return [{"activity": "com.x.ui.HomeActivity", "controls": controls}]


def test_shared_rid_labels_merge_into_one_line_in_dump_order():
    """同一 rid 的多个文案 → 一行；顺序 = **首次出现顺序**（不是按坐标排）。"""
    pages = _page([
        {"res-id": "com.x:id/channelbar_text", "text": "7x24", "bounds": "[0,221][153,318]"},
        {"res-id": "com.x:id/channelbar_text", "text": "要闻", "bounds": "[153,221][291,318]"},
        {"res-id": "com.x:id/channelbar_text", "text": "国际", "bounds": "[705,221][843,318]"},
    ])
    ctx = controls_context(pages)
    rid_lines = [ln for ln in ctx.splitlines() if "channelbar_text" in ln]
    assert len(rid_lines) == 1, f"同一 rid 应合并成一行：\n{ctx}"
    line = rid_lines[0]
    assert "7x24 | 要闻 | 国际" in line, line          # 顺序按首次出现
    assert "共用 rid" in line and "3 个文案" in line   # 明确告诉模型这是共用容器


def test_single_label_rid_keeps_the_old_shape():
    """单文案的 rid 不加提示：它不制造歧义，加了只是每行多 ~20 字符。"""
    pages = _page([
        {"res-id": "com.x:id/ok", "text": "确定", "bounds": "[0,0][10,10]"},
        {"res-id": "com.x:id/ok", "text": "确定", "bounds": "[0,0][10,10]"},  # 重复快照
    ])
    ctx = controls_context(pages)
    assert "- res-id: com.x:id/ok  文案: 确定  页面: com.x.ui.HomeActivity" in ctx
    assert "共用 rid" not in ctx


def test_merged_labels_are_capped_so_one_line_cannot_eat_the_budget():
    """某社区类 App 有 rid 被 30+ 个文案共用：全列出来单行上千字符，必须折成「等 N 个」。"""
    pages = _page([
        {"res-id": "com.x:id/tag", "text": f"标签{i}", "bounds": f"[0,{i}][10,{i + 1}]"}
        for i in range(12)
    ])
    ctx = controls_context(pages)
    line = next(ln for ln in ctx.splitlines() if "com.x:id/tag" in ln)
    assert "标签0 | 标签1 | 标签2 | 标签3 | 标签4 | 标签5 | 标签6 | 标签7" in line
    assert "等 12 个" in line
    assert "标签11" not in line          # 超出的不列，只报总数


def test_label_less_multi_instance_is_marked_with_count():
    """无文案的容器（列表/卡片容器）要报出实例数——位置型意图才可能用 index。"""
    pages = _page([
        {"res-id": "com.x:id/list_container", "bounds": "[0,100][100,200]"},
        {"res-id": "com.x:id/list_container", "bounds": "[0,200][100,300]"},
    ])
    ctx = controls_context(pages)
    line = next(ln for ln in ctx.splitlines() if "list_container" in ln)
    assert "无文案 ×2" in line, line


# -- R13 第 ② 半（2026-10-28）：容器 index→文案 映射 -------------------------------------
#
# 第 ① 半（合并成一行）只解决"看不出是共用容器"，解决不了**"res-id 指定不到具体 tab"**：
# `text=国际` 在首页有 3 个同名（2 个 tab 的两份快照 + 1 个筛选弹窗标签），R21 报"改用 res-id"，
# 而 `channelbar_text` 一个 rid 管 9 个 tab ⇒ 写 res-id 等于点第一个 ⇒ **建议不可执行**。
# 实测该页 `channel_bar_item_container`（可点、无文案、15 实例）与 `channelbar_text` 的
# 15 个实例 **bounds 逐一完全相同** ⇒ 才能给出准确的 `index → 文案`。

_CHANNEL_BOUNDS = ["[0,221][153,318]", "[153,221][291,318]", "[705,221][843,318]"]


def _channel_pages(container_bounds=None, container_clickable: bool = True) -> list[dict]:
    controls = [{"res-id": "com.x:id/channelbar_text", "text": t, "bounds": b}
                for t, b in zip(("7x24", "要闻", "国际"), _CHANNEL_BOUNDS, strict=True)]
    for b in (container_bounds or _CHANNEL_BOUNDS):
        ctl = {"res-id": "com.x:id/channel_bar_item_container", "bounds": b}
        if container_clickable:
            ctl["clickable"] = True
        controls.append(ctl)
    return _page(controls)


def test_container_gets_index_to_label_map_when_bounds_align():
    """bounds 一一对齐的可点容器 ⇒ 给出 `index→文案`，模型才写得出 `res-id + index`。"""
    ctx = controls_context(_channel_pages())
    line = next(ln for ln in ctx.splitlines() if "channel_bar_item_container" in ln)
    assert "index→文案: 0=7x24 1=要闻 2=国际" in line, line
    assert "实例序号" in line


def test_container_without_aligned_bounds_gets_no_map():
    """bounds 对不上 ⇒ 不猜映射（错映射比没有更糟：会把 index 指到别的控件）。"""
    ctx = controls_context(_channel_pages(container_bounds=["[0,0][9,9]"] * 3))
    line = next(ln for ln in ctx.splitlines() if "channel_bar_item_container" in ln)
    assert "index→文案" not in line, line


def test_container_that_is_not_clickable_gets_no_map():
    """index 指向一个不可点的 View 对点击没用 ⇒ 不给映射。"""
    ctx = controls_context(_channel_pages(container_clickable=False))
    line = next(ln for ln in ctx.splitlines() if "channel_bar_item_container" in ln)
    assert "index→文案" not in line, line


def test_multi_label_hint_no_longer_says_use_index_in_this_order():
    """防回归：文案行列的是**去重文案**（9 个），index 数的是**实例**（15 个）⇒
    "按此顺序用 index" 是错的，照它算必偏移。"""
    ctx = controls_context(_channel_pages())
    line = next(ln for ln in ctx.splitlines() if "channelbar_text" in ln)
    assert "按此顺序用 index" not in line, line
    assert "可点容器" in line, line


# -- 退化 res-id（`0_resource_name_obfuscated`）当「没有 rid」处理（2026-10-28）-------------
#
# 真机事实（xhs 首页 dump）：底部导航的 `首页`/`我`/`发布`/`消息，3条未读` **文案在
# content-desc 里、clickable=true**，而 res-id 全是 `0_resource_name_obfuscated`（四个 tab
# 共用）。旧实现 `if rid: … continue` 把这类控件**整条丢掉**（文案一起丢）⇒
# ① 控件清单里一个底部导航都没有（55 条），R18 把模型写**对**的 `text=我` 报成"不在清单"；
# ② 候选集里多出 48 行"共用 rid、8~36 个互不相关文案"的噪声行（标题/点赞数/用户名），
#    约占 xhs 整份清单 58% 的字符，且完全无法定位。

_DEGEN = "com.x:y:id/0_resource_name_obfuscated"


def _degenerate_page() -> list[dict]:
    return _page([
        {"res-id": _DEGEN, "desc": "首页", "clickable": True, "bounds": "[0,2079][216,2214]"},
        {"res-id": _DEGEN, "desc": "我", "clickable": True, "bounds": "[864,2079][1080,2214]"},
        {"res-id": "com.x:id/index_store", "desc": "市集", "clickable": True,
         "bounds": "[216,2079][432,2214]"},
        # 退化 rid 的不可点内容控件：不该进清单（只收可点），也不该产生 rid 行
        {"res-id": _DEGEN, "text": "点赞 15", "bounds": "[0,1000][100,1050]"},
        {"res-id": _DEGEN, "text": "东胜野生眉安妮", "bounds": "[0,1100][100,1150]"},
    ])


def test_degenerate_rid_falls_back_to_text_in_candidate_list():
    """退化 rid 的**可点**控件要能进候选集（否则底部导航永远写不出来）。"""
    ctx = controls_context(_degenerate_page())
    assert "- 文案: 首页  类型: clickable" in ctx, ctx
    assert "- 文案: 我  类型: clickable" in ctx, ctx


def test_degenerate_rid_never_produces_a_rid_line():
    """退化 rid 不许产出 rid 行：它零区分度，那行只会把无关文案堆在一起。"""
    ctx = controls_context(_degenerate_page())
    assert "0_resource_name_obfuscated" not in ctx, ctx


def test_degenerate_rid_does_not_swallow_the_real_rid_neighbour():
    """有真名的同页控件（`index_store`=市集）不受影响，仍按 res-id 给出。"""
    ctx = controls_context(_degenerate_page())
    line = next(ln for ln in ctx.splitlines() if "index_store" in ln)
    assert "文案: 市集" in line, line


# -- 「部分动态」文案不进候选集（2026-10-28，放开退化 rid 后立刻踩到的回归）------------------
#
# xhs 底部导航的「消息」tab 只有 `content-desc='消息，1条未读'`（可点）+ `text='消息'`（不可点）。
# 放开退化 rid 之后前者进了候选集，模型**3/3 照抄** `text=消息，1条未读` ⇒ 未读数一变就定位失败。
# `is_dynamic_text` 拦不住它（那套正则都是整串锚定，`消息，1条未读` 开头是固定文案）。


def test_partial_dynamic_label_is_filtered_from_candidates():
    from insta_engine.page_index import has_dynamic_segment

    pages = _page([
        {"res-id": _DEGEN, "desc": "消息，1条未读", "clickable": True,
         "bounds": "[648,2079][864,2214]"},
        {"res-id": _DEGEN, "desc": "我", "clickable": True, "bounds": "[864,2079][1080,2214]"},
    ])
    ctx = controls_context(pages)
    assert "1条未读" not in ctx, ctx
    assert "- 文案: 我  类型: clickable" in ctx, ctx      # 稳定文案不许被连坐
    assert has_dynamic_segment("消息，1条未读") is True
    assert has_dynamic_segment("消息") is False


def test_partial_dynamic_rule_does_not_touch_page_key_semantics():
    """`has_dynamic_segment` **不进** `is_dynamic_text`：后者是 page_key 用的，
    扩大错杀面会让两个不同页面撞 key（那里写明"宁可漏杀不可错杀"）。"""
    from insta_engine.page_index import has_dynamic_segment, is_dynamic_text

    assert is_dynamic_text("消息，1条未读") is False      # page_key 侧保持原语义
    assert has_dynamic_segment("消息，1条未读") is True   # 候选集侧才拦


# ---------------------------------------------------------------------------
# 补采 `selected` / `checked`（2026-10-28）
#
# 为什么：切 tab 的正确断言是 `{"type":"selected", ...}`，但**控件字典原先只有 6 个字段**
# （class/text/desc/res-id/clickable/bounds）⇒ 图谱全量 0 个 `selected=True`，
# 候选集里"哪些控件真的带 selected 可断言"无从得知，模型只能猜；猜错就是运行期
# `get_state` 抛"节点无 selected 状态"⇒ 断言必红。
# 实测 xhs 底部「消息」tab 的容器（`desc='消息，3条未读'`）与 TextView（`text='消息'`）
# 都带 `selected=true`，图谱里一个都没有。
# ---------------------------------------------------------------------------

_XML_HEAD = ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
             '<hierarchy rotation="0">\n')


def _node(*, text="", desc="", rid="", cls="android.widget.TextView",
          clickable="false", selected="false", checked="false",
          bounds="[0,0][10,10]", package="com.x") -> str:
    return (f'  <node index="0" text="{text}" resource-id="{rid}" class="{cls}" '
            f'package="{package}" content-desc="{desc}" checkable="false" '
            f'checked="{checked}" clickable="{clickable}" enabled="true" '
            f'focusable="false" focused="false" scrollable="false" '
            f'long-clickable="false" password="false" selected="{selected}" '
            f'bounds="{bounds}" />\n')


def _xml(*nodes: str) -> str:
    return _XML_HEAD + "".join(nodes) + "</hierarchy>"


def test_digest_page_captures_selected_and_checked():
    from insta_engine.page_index import digest_page

    xml = _xml(
        _node(rid="com.x:id/tab", desc="消息", clickable="true", selected="true",
              bounds="[0,2000][100,2100]"),
        _node(rid="com.x:id/sw", clickable="true", checked="true",
              bounds="[0,100][100,200]"),
        _node(rid="com.x:id/plain", text="普通", bounds="[0,300][100,400]"),
    )
    ctl = {c["res-id"]: c for c in digest_page(xml, "com.x")["controls"]}
    assert ctl["com.x:id/tab"].get("selected") is True
    assert ctl["com.x:id/sw"].get("checked") is True
    # 反向：没有状态的控件**不许**多出键（否则每份图谱白涨体积）
    assert "selected" not in ctl["com.x:id/plain"]
    assert "checked" not in ctl["com.x:id/plain"]


def test_digest_page_omits_false_state():
    """`selected="false"` 不写键——与 `clickable` 同口径（只在 True 时写）。"""
    from insta_engine.page_index import digest_page

    xml = _xml(_node(rid="com.x:id/a", text="甲", selected="false", checked="false"))
    ctl = digest_page(xml, "com.x")["controls"][0]
    assert "selected" not in ctl and "checked" not in ctl


def test_page_key_ignores_selected():
    """**关键**：`selected` 是**状态**，不能进 `page_key`。

    否则同一页在「7x24 选中」与「视频选中」两帧下会算出两个 key ⇒ 每次切 tab 都
    变成"新页面"，图谱按页数爆炸、走链全断。
    """
    from insta_engine.page_index import page_key

    a = _xml(_node(rid="com.x:id/t", text="7x24", selected="true",
                   bounds="[0,221][153,318]"))
    b = _xml(_node(rid="com.x:id/t", text="7x24", selected="false",
                   bounds="[0,221][153,318]"))
    assert page_key(a, "com.x") == page_key(b, "com.x")


def test_rid_line_marks_real_selected_for_single_instance():
    from insta_engine.page_index import controls_context

    pages = _page([{"res-id": "com.x:id/news_tab", "desc": "新闻", "clickable": True,
                    "selected": True, "bounds": "[0,100][100,200]"}])
    ctx = controls_context(pages)
    line = next(ln for ln in ctx.splitlines() if "news_tab" in ln)
    assert "★真机 selected" in line, line
    assert "index=" not in line.split("★")[1], line   # 单实例不写 index，免得误导


def test_rid_line_marks_which_instance_is_selected():
    """多实例：要给出**当前选中的实例序号**——那是"切换前"的基线。"""
    from insta_engine.page_index import controls_context

    pages = _page([
        {"res-id": "com.x:id/cb", "text": "7x24", "bounds": "[0,221][153,318]"},
        {"res-id": "com.x:id/cb", "text": "要闻", "bounds": "[153,221][291,318]"},
        {"res-id": "com.x:id/cb", "text": "视频", "bounds": "[429,221][567,318]",
         "selected": True},
    ])
    ctx = controls_context(pages)
    line = next(ln for ln in ctx.splitlines() if "com.x:id/cb" in ln)
    assert "★真机 selected: index=2" in line, line


def test_no_selected_no_marker():
    """没采到 selected 的页**不许**出现标记——否则等于告诉模型"这里能断 selected"。"""
    from insta_engine.page_index import controls_context

    pages = _page([{"res-id": "com.x:id/cb", "text": "7x24", "bounds": "[0,221][153,318]"}])
    ctx = controls_context(pages)
    assert "★" not in ctx
