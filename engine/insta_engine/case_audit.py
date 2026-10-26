"""用例定位审计（**只保留与 `case_quality` 不重叠的两条**：L1 / L6）。

## 与 `case_quality` 的分工（2026-10-13 合并 `feature/console-v2` 时定）

本模块原为六条（L1–L6），与 main 的 `case_quality` **重叠了四条** ⇒ 合并时只保留独有的两条，
重叠的删掉（否则同一个缺陷会被两套各报一次）：

| 原规则 | 现在由谁管 |
|---|---|
| L1 清单校验（text/res-id 是否命中控件清单） | **本模块**（`R18`）—— `case_quality` 判不了（它拿不到页面索引） |
| L2 无障碍注解（`视频，可选中`） | `case_quality` 的 **R5**（确定性剥离，比"只提示"更进一步） |
| L3 动态内容（超长/强标点） | `case_quality` 的 **R4** |
| L4 断言自指（点完又断言同一控件） | `case_quality` 的 **R2**（且对所有策略生效） |
| L5 首步可达性 | `case_quality` 的 **R6**（缺 launch 自动补 + 前置条件分类） |
| L6 一次性系统弹窗 | **本模块**（`R19`）—— `case_quality` 判不了（要认弹窗按钮词） |

## 为什么 L1 只警告、且索引缺失时**整条跳过**

`index_dir` 缺省、或目录里没有该包的索引时，L1 **不猜**（直接跳过）：控件清单是**快照**，
索引里没探到的页面其 res-id 自然不在集合里 ⇒ 硬判会把"有效但还没探到"的报成编造。
（这条判据在 main 侧曾被评估过并**否掉**，理由正是这个误伤面；保留为**警告**是可接受的折中。）
"""

from __future__ import annotations

import re
from pathlib import Path

from .case_quality import STATE_ASSERTION_TYPES, fmt_issue
from .dsl import Action, AssertionType, LocatorStrategy, TestCase
from .page_index import controls_list_for, load_index, pages_equivalent

#: L6（R19）：系统/首启权限弹窗的常见按钮词（全文精确匹配，避免「同意条款」类误报）
_PERMISSION_WORDS = frozenset({
    "同意", "拒绝", "允许", "不允许", "始终允许", "仅本次允许",
    "仅在使用中允许", "禁止", "不再询问", "始终禁止",
})
_STATEFUL_TITLE_RE = re.compile(r"权限|授权|弹窗")

_TEXT_LIKE = (LocatorStrategy.TEXT, LocatorStrategy.TEXT_CONTAINS, LocatorStrategy.DESCRIPTION)
_ID_LIKE = (LocatorStrategy.RES_ID, LocatorStrategy.ID)

#: 会让页面发生变化的动作（用于沿图谱推"这一步在哪一页"）
_FORWARD_ACTIONS = (Action.TAP, Action.LONG_PRESS)
#: 页面会变但**变到哪**不可知的动作（返回/回桌面）——推不出来就停止判定，不猜
_UNKNOWN_ACTIONS = (Action.BACK, Action.HOME)
#: 「一次性启动噪声」的 res-id 形态（与 `case_quality._DIALOG_CONFIRM_RE` 同义，
#: 这里独立一份是为了不让审计模块依赖质量闸门的私有常量）。
_NOISE_RID_RE = re.compile(
    r"(?i)(?:dialog|popup|alert)[_./-]*(?:confirm|ok|agree|allow|positive)"
    r"|(?:confirm|agree)[_./-]*(?:btn|button|dialog)")


def _is_noise_tap(label: str) -> bool:
    """这一步是不是「关掉启动噪声」（跳过广告 / 首启弹窗确认）。

    走链时必须把它当成**页面不变**：它不是导航，点完还停在同一页。
    否则走链在第一步就断（`s2 tap 跳过` 找不到边 ⇒ 之后所有步骤的页面都未知），
    R20/R21 全程弃权——2026-10-21 实测：tx-news 4 条频道切换用例就是这样一条都报不出来。
    """
    from .safety import is_splash_skip_label

    text = (label or "").strip()
    return bool(text) and (is_splash_skip_label(text) or bool(_NOISE_RID_RE.search(text)))


#: 沿图谱走链的上限（防止超长用例把审计拖慢）
_MAX_WALK = 40

#: R24：断言目标的**页面区分度**下限。一个文案若在 ≥`RATIO` 的页面里都出现
#: （且至少 `MIN_PAGES` 页），它就不能用来证明"到了目标页"。
#: 为什么用相对比例：某资讯 App 53 页时「要闻」出现 15 页（28%），某生活服务 App 223 页时
#: 「我的」出现 140 页（63%）——绝对阈值在两边不可比（碎片化程度差 4 倍）。
NONDISCRIMINATING_RATIO = 0.10
NONDISCRIMINATING_MIN_PAGES = 5


def audit_case(case: TestCase, index_dir: Path | None = None) -> list[str]:
    """对单条用例跑 L1/L6 审计，返回**带规则编号**的警告列表（只警告不阻断）。

    `index_dir`：page_index 目录（`root/artifacts/page_index`）。缺省 None 或目录里
    没有该包的索引时，L1 自动跳过（见模块说明），L6 为纯规则不依赖索引。
    """
    pkg = (case.target or {}).get("package", "")
    known_res: set[str] = set()
    list_texts: set[str] = set()
    if index_dir is not None and pkg:
        # ⚠️ **必须不截断**（`max_controls=None`）：R18 是**存在性判定**，用展示用的
        # 前 300 条去判"不在清单里"，等于把没截到的页面全判成编造。实测某资讯类 App 1499 个
        # 控件，前 300 条只覆盖首页 1 个 activity ⇒ 详情页的 news_detail_title /
        # action_bar_comment / title_bar_share_btn 全被误报（它们在真机上确实存在，
        # 同批用例 3/3 通过）。
        items = controls_list_for(pkg, index_dir, max_controls=None)
        known_res = {it["res_id"] for it in items if it["res_id"]}
        list_texts = {it["label"] for it in items if it["label"]}

    def _audit_loc(prefix: str, loc) -> list[str]:
        """单条定位的 L1（R18）。"""
        out: list[str] = []
        if loc is None:
            return out
        value = (loc.value or "").strip()
        if not value:
            return out
        if loc.strategy in _TEXT_LIKE and list_texts and value not in list_texts:
            out.append(fmt_issue("R18", f"{prefix}：文案「{value}」不在控件清单的可点文案中，"
                                         "请对照控件清单确认控件文本"))
        elif loc.strategy in _ID_LIKE and known_res and value not in known_res:
            out.append(fmt_issue("R18", f"{prefix}：res-id「{value}」不在控件清单，"
                                         "可能拼写有误（也可能是索引尚未覆盖该页）"))
        return out

    warns: list[str] = []
    for st in case.steps:
        label = f"步骤 {st.id}"
        if st.target is not None:
            warns.extend(_audit_loc(label, st.target))
        for i, a in enumerate(st.assertions, 1):
            if a.target is not None:
                warns.extend(_audit_loc(f"{label} 断言{i}", a.target))
        # L6（R19）：一次性系统/首启弹窗依赖
        if (st.action == Action.TAP and st.target is not None
                and st.target.strategy == LocatorStrategy.TEXT
                and (st.target.value or "").strip() in _PERMISSION_WORDS
                and (_STATEFUL_TITLE_RE.search(case.name or "")
                     or _STATEFUL_TITLE_RE.search(st.intent or ""))):
            warns.append(fmt_issue(
                "R19", f"{label}：点击「{(st.target.value or '').strip()}」疑似系统/首启权限弹窗按钮"
                       "——弹窗仅在首次触发时出现，重跑通常不再弹出；"
                       "建议人工执行或先重置授权状态"))

    # L7（R20）：**本步所在页**是否有这条定位；R21：文案定位不唯一/只有 desc
    if index_dir is not None and pkg:
        warns.extend(_cross_page_issues(case, index_dir, pkg))
        warns.extend(_ambiguous_text_issues(case, index_dir, pkg))
        warns.extend(nondiscriminating_assertions(case, index_dir, pkg))
    return warns


def _locator_values(loc) -> list[tuple[str, str]]:
    """定位链上所有 (strategy, value)（fallback 逐级降级，都要能认出来）。"""
    out: list[tuple[str, str]] = []
    cur = loc
    while cur is not None:
        out.append((str(getattr(cur, "strategy", "")), str(getattr(cur, "value", "") or "")))
        cur = getattr(cur, "fallback", None)
    return out


def _page_has(page: dict, loc) -> bool:
    """该页的控件清单里是否有这条定位值。"""
    value = (loc.value or "").strip()
    if not value:
        return False
    for ctl in page.get("controls") or []:
        if loc.strategy in _TEXT_LIKE and str(ctl.get("text") or ctl.get("desc") or "") == value:
            return True
        if loc.strategy in _ID_LIKE and str(ctl.get("res-id") or "") == value:
            return True
    return False


def _equiv_group(page: dict, pages: list[dict]) -> list[dict]:
    """该页 + 它的**同逻辑页碎片**（`pages_equivalent`）。

    某生活服务 App实测 177 页里 97 页含「外卖」文案——只按 page_key 判会让同一个逻辑页的
    另一个碎片被误报成"跨页"。按等价组判，宁可漏报不可误报。
    """
    return [page] + [p for p in pages if p is not page and pages_equivalent(p, page)]


def _walk_expected_pages(case: TestCase, pages: list[dict],
                         extra_edges: list[dict] | None = None) -> list[tuple[dict | None, dict | None]]:
    """沿图谱推每一步的「动作前所在页 / 动作后所在页」。

    规则：
    - 起点 = 控件最多的无父页（launch 落点）；
    - `tap`/`long-press`：在图谱里找 `from=当前页 且 action=本步定位值` 的边，跟着走；
      找不到边 ⇒ 从这一步起**放弃判定**（页面未知，不猜）；
    - `back`/`home`：页面变了但不知道变到哪 ⇒ 放弃判定；
    - 其余动作（input/clear/scroll/wait/swipe）：页面不变。
    """
    by_key = {p["page_key"]: p for p in pages if p.get("page_key")}
    roots = [p for p in pages if not p.get("parent_key")]
    cur: dict | None = max(roots, key=lambda p: p.get("node_count") or 0) if roots else None
    edges: dict[str, list[dict]] = {}
    for e in (case_quality_edges(pages, extra_edges) or []):
        edges.setdefault(e["from"], []).append(e)

    out: list[tuple[dict | None, dict | None]] = []
    for idx, st in enumerate(case.steps):
        if idx >= _MAX_WALK:
            out.append((None, None))
            continue
        before = cur
        after = cur
        if cur is not None:
            if st.action in _UNKNOWN_ACTIONS:
                after = None
            elif st.action in _FORWARD_ACTIONS and st.target is not None \
                    and _is_noise_tap(st.target.value or ""):
                after = cur          # 关广告/关弹窗不换页
            elif st.action in _FORWARD_ACTIONS and st.target is not None:
                # 按**文案或 res-id** 认边：icon-only 控件没有文案，只能用 rid 重放
                # （explore 从 2026-10 起记 `parent_rid`；老图谱该字段为空，走文案）。
                values = {v for _st, v in _locator_values(st.target) if v}
                nxt = next((e["to"] for e in edges.get(cur.get("page_key"), [])
                            if e.get("action") in values or e.get("rid") in values), None)
                after = by_key.get(nxt) if nxt else None
            cur = after
        out.append((before, after))
    return out


def _page_label(page: dict) -> str:
    """页面的人类可读名：标题 > activity 短名 > page_key 前 8 位。"""
    title = (page.get("title") or "").strip()
    if title:
        return title
    act = (page.get("activity") or "").strip()
    if act:
        return act.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return (page.get("page_key") or "")[:8]


def _page_candidates(page: dict, limit: int = 6, clickable_only: bool = False) -> list[str]:
    """该页可用的定位候选（res-id 优先，其次稳定文案），供生成期回喂。"""
    out: list[str] = []
    for ctl in page.get("controls") or []:
        if clickable_only and not ctl.get("clickable"):
            continue
        rid = str(ctl.get("res-id") or "").strip()
        text = str(ctl.get("text") or ctl.get("desc") or "").strip()
        if rid:
            out.append(f"res-id={rid}" + (f"（文案「{text[:20]}」）" if text else ""))
        elif text:
            out.append(f"text={text[:20]}")
        if len(out) >= limit:
            break
    return out


def page_conflicts(case: TestCase, index_dir: Path | str, pkg: str) -> list[dict]:
    """定位写在了**别的页面**上的冲突（结构化，供 R20 与生成期闸门共用）。

    判据（与 R20 同一套，**宁可漏报不可误报**）：
      · 该定位值在全图谱**存在**（全图谱都没有是 R18 的事，不重复）；
      · 沿图谱走链能确定**本步所在页**；
      · 该页（含 `pages_equivalent` 的同逻辑碎片组）**没有**这条定位。

    为什么需要它（2026-10-21 真机实测，某生活服务 App 3 条同代用例 0/3）：
    用例 `s3 wait res-id=txt_address_info` 通过了 R18（该 res-id 在外卖页 6 个碎片上都有），
    但那条用例的 `s2` 已把页面导航到「地址选择页」——断言写的是**上一页**的控件。
    R18 的粒度是"全图谱"，太宽；R20 把粒度收到"本步所在页"，而**生成期就该拦**，
    不该等真机跑完才发现。
    """
    index = load_index(Path(index_dir) / f"{pkg}.json")
    pages = [p for p in (index or {}).get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    if not pages:
        return []                       # 没图谱不判（同 R18 的降级纪律）
    extra_edges = (index or {}).get("edges") or []
    all_has = lambda loc: any(_page_has(p, loc) for p in pages)  # noqa: E731

    out: list[dict] = []
    for st, (before, after) in zip(case.steps, _walk_expected_pages(case, pages, extra_edges),
                                     strict=False):
        checks: list[tuple[str, object, dict, bool]] = []
        # **噪声步骤不参与页面校验**：`跳过`/弹窗确认是一次性控件，本来就不在任何页面的
        # 控件清单里（它们只在开屏/弹窗那一帧存在）⇒ 拿"本步所在页"去校验必然误报。
        if (st.target is not None and before is not None
                and not _is_noise_tap(st.target.value or "")):
            checks.append((f"步骤 {st.id} 的定位", st.target, before, True))
        for i, a in enumerate(st.assertions, 1):
            # 断言通常针对**动作之后**的页面（点完到哪一页）
            page = after if after is not None else before
            if a.target is not None and page is not None:
                checks.append((f"步骤 {st.id} 断言{i}", a.target, page, False))
        for label, loc, page, is_action in checks:
            value = (loc.value or "").strip()
            if not value or not all_has(loc):
                continue                # 全图谱都没有 ⇒ R18 已报，不重复
            group = _equiv_group(page, pages)
            if any(_page_has(p, loc) for p in group):
                continue
            out.append({"label": label, "value": value,
                        "strategy": str(getattr(loc, "strategy", "")),
                        "page": _page_label(page),
                        "activity": (page.get("activity") or "").strip(),
                        "candidates": _page_candidates(page, clickable_only=is_action)})
    return out


def _rid_match_count(page: dict, value: str) -> int:
    """该页里 res-id 恰等于 value 的控件数（同一 rid 常被多个控件共用）。"""
    return sum(1 for ctl in page.get("controls") or []
               if str(ctl.get("res-id") or "").strip() == value)


def _text_match_kinds(page: dict, value: str) -> tuple[int, int]:
    """该页里 text 恰等于 value 的控件数、desc 恰等于 value 的控件数。"""
    by_text = by_desc = 0
    for ctl in page.get("controls") or []:
        text = str(ctl.get("text") or "").strip()
        desc = str(ctl.get("desc") or "").strip()
        if text and text == value:
            by_text += 1
        if desc and desc == value:
            by_desc += 1
    return by_text, by_desc


def _ambiguous_text_issues(case: TestCase, index_dir: Path, pkg: str) -> list[str]:
    """R21：`text` 定位在该页**不唯一**，或该页只有 content-desc 同名控件（图标入口）。

    为什么（2026-10-21 真机实测，某生活服务 App「外卖」用例）：用例 `s2 tap text=外卖`，首页上真正的
    入口是 `desc="外卖"` 的图标 View，而商品卡上还有 `text="外卖"` 的角标。`locator._try`
    的 TEXT 策略链是 **text 精确 → description 精确 → 包含** ⇒ text 精确先命中角标，
    点开的是商品页/搜索页，用例在**错误的页面**上继续，而引擎当时**没有任何提示**——
    失败只表现为下一步"定位失败"，归因时被记成"图谱/文案过期"。
    """
    index = load_index(Path(index_dir) / f"{pkg}.json")
    pages = [p for p in (index or {}).get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    if not pages:
        return []
    extra_edges = (index or {}).get("edges") or []
    text_like = {LocatorStrategy.TEXT.value, LocatorStrategy.TEXT_CONTAINS.value}

    warns: list[str] = []
    for st, (before, _after) in zip(case.steps, _walk_expected_pages(case, pages, extra_edges),
                                     strict=False):
        if st.target is None or before is None:
            continue
        strategy = str(getattr(st.target, "strategy", ""))
        value = (st.target.value or "").strip()
        if not value or _is_noise_tap(value):
            continue
        if strategy in _ID_LIKE:
            # **res-id 也会不唯一**（2026-10-21 真机实测）：某资讯 App 7 个频道 tab
            # （7x24/要闻/关注/视频/天津/国际/娱乐）共用 `channelbar_text`
            # ⇒ 用例写 `res-id=channelbar_text` 时永远命中第一个 tab，点到的不是想要的那个，
            # 而每一步都报"成功"。这类错误比文案歧义更隐蔽（res-id 看起来"最精确"）。
            hits = max((_rid_match_count(pg, value) for pg in _equiv_group(before, pages)),
                       default=0)
            if hits > 1:
                warns.append(fmt_issue(
                    "R21", f"步骤 {st.id}：res-id「{value}」在**本步所在页**有 {hits} 个控件共用"
                           "——运行期会命中第一个，点到的不是想要的那个（每一步都报成功）；"
                           "建议改用 text 或把范围缩到具体控件"))
            continue
        if strategy not in text_like:
            continue
        # ⚠️ 取**最大值**而不是求和：`_equiv_group` 是同一逻辑页的多个碎片，求和会把
        # 同一批控件重复计入（实测求和得出"38 个同名控件"这种荒谬数字）。
        by_text = by_desc = 0
        for page in _equiv_group(before, pages):
            a, b = _text_match_kinds(page, value)
            by_text = max(by_text, a)
            by_desc = max(by_desc, b)
        if by_text > 1:
            warns.append(fmt_issue(
                "R21", f"步骤 {st.id}：文案「{value}」在**本步所在页**有 {by_text} 个同名控件"
                       "——text 定位会命中第一个，点错也不会报错；建议改用 res-id"))
        elif by_text >= 1 and by_desc >= 1:
            # 真机实测（某生活服务 App「外卖」）：入口是 desc=外卖 的图标 View，商品卡上还有 text=外卖 的角标
            warns.append(fmt_issue(
                "R21", f"步骤 {st.id}：文案「{value}」在该页**同时存在 text 与 content-desc 两种同名控件**"
                       "（图标入口常把文案放在 desc）——TEXT 策略是 text 精确优先，运行期会先命中"
                       "内容标签（如商品角标/筛选条），点开别的页面；建议改用 res-id"))
        elif by_text == 0 and by_desc >= 1:
            warns.append(fmt_issue(
                "R21", f"步骤 {st.id}：文案「{value}」在该页只匹配到 **content-desc** 控件"
                       "（图标入口）——TEXT 策略是 text 精确优先，运行期会先命中同名的内容标签"
                       "（如商品角标），点开别的页面；建议改用 res-id"))
    return warns


def _rid_label_groups(pages: list[dict]) -> dict[str, set[str]]:
    """res-id → 它覆盖的**不同文案**集合（判"容器 rid"用）。

    一个 rid 对应 7 个不同文案（7x24/要闻/关注/视频/天津/国际/娱乐）⇒ 它不是"一个控件"，
    而是**一组控件的容器**。断言它的状态只能反映组里第一个，与被点的那一个无关。
    """
    groups: dict[str, set[str]] = {}
    for page in pages:
        for ctl in page.get("controls") or []:
            rid = str(ctl.get("res-id") or "").strip()
            label = str(ctl.get("text") or ctl.get("desc") or "").strip()
            if rid and label:
                groups.setdefault(rid, set()).add(label)
    return groups


#: 状态类断言：它们断的是"状态变了"，即使目标元素在动作前就存在**也仍然有意义**
#: （`selected 我的` 是语料里最好的一条断言：底部 tab 一直在，但选中态会变）。
_STATE_ASSERTIONS = frozenset({
    AssertionType.SELECTED, AssertionType.CHECKED,
    AssertionType.ENABLED, AssertionType.DISABLED,
})


def nondiscriminating_assertions(case: TestCase, index_dir: Path | str,
                                 pkg: str) -> list[str]:
    """R24：**用来证明"到了某页"的断言，在动作之前就已经成立**（只报不拦）。

    判据（两条，都靠页面索引，不需要真机）：

    | 形态 | 判据 | 实测 |
    |---|---|---|
    | **动作前已成立** | 非状态断言的目标，在**这一步动作之前所在的那一页**就存在 | `tap 要闻` 前「要闻」就在首页频道栏里 ⇒ 切没切都成立 |
    | **容器 rid** | 断言目标 rid 在全图谱对应 **>1 个不同文案** | `selected channelbar_text` 覆盖 7 个 tab，只反映第一个（7x24，首页本来就选中）|

    ## 为什么是"动作前那页"而不是"全图谱出现多少页"（2026-10-22 试过，错了）

    第一版用的是"目标文案在 ≥10% 的页面出现就报"。在某内容社区 App上**误报**：`搜索,` 出现在
    62 页，但那 62 页**全是搜索页的各种碎片**——它恰恰**能**证明"到了搜索页"。
    反过来，`要闻` 只出现在 4 个逻辑页里（低于阈值），却**真的**判不出频道切换。

    ⇒ 判据必须是**状态转移**：动作之前成不成立，而不是它有多常见。
    （`pages_equivalent` 把碎片并成逻辑页也救不了：要闻和首页是**同一页**，合并后更看不出来。）

    ## 为什么状态断言不走第一条

    `selected` / `checked` 这类断的是**状态变化**：元素一直在，但选中态会变 ⇒ 有区分度。
    实测踩过：把状态断言也按"动作前已成立"报，会误伤语料里最好的一条（`selected 我的`）。

    ## 覆盖边界（必须一起报）

    第一条依赖**沿图谱走链**（`_walk_expected_pages`）。走链断了就**弃权**（不猜），
    所以"没报 R24"不等于"断言没问题"——要看 `page_guard_coverage` 的可判率。
    """
    index = load_index(Path(index_dir) / f"{pkg}.json")
    pages = [p for p in (index or {}).get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    if not pages:
        return []
    groups = _rid_label_groups(pages)
    walk = _walk_expected_pages(case, pages, (index or {}).get("edges") or [])
    pre_nav: dict | None = None
    pre_hits: list[tuple[str, str, str]] = []
    containers: list[tuple[str, str, int]] = []
    for st, (before, _after) in zip(case.steps, walk, strict=False):
        if st.action == Action.LAUNCH:
            pre_nav = None
        elif st.action in _FORWARD_ACTIONS or st.action in _UNKNOWN_ACTIONS \
                or st.action == Action.SWIPE:
            pre_nav = before if before is not None else None
        for a in st.assertions:
            if a.target is None:
                continue
            value = (a.target.value or "").strip()
            if not value:
                continue
            labels = groups.get(value)
            if labels and len(labels) > 1:
                containers.append((st.id, value, len(labels)))
            elif pre_nav is not None and a.type not in _STATE_ASSERTIONS \
                    and _page_has(pre_nav, a.target):
                pre_hits.append((st.id, value, str(pre_nav.get("title") or pre_nav.get("page_key"))))
    warns: list[str] = []
    if pre_hits:
        shown = "、".join(f"`{v}`（{page}）" for _, v, page in pre_hits[:4])
        warns.append(fmt_issue(
            "R24", f"断言在**动作之前就成立**：{shown}——这些目标在点击前所在的那一页上就有，"
                   "断言它「存在」证明不了页面变了（App 没反应也照样绿）。"
                   "建议改断**状态**（`selected`/`checked`）或**目标页独有**的业务控件"))
    if containers:
        shown = "、".join(f"`{v}`（对应 {n} 个不同文案）" for _, v, n in containers[:4])
        warns.append(fmt_issue(
            "R24", f"断言目标是**容器 res-id**：{shown}——这类 rid 覆盖一组控件，"
                   "断言它的状态只反映组里**第一个**，与被操作的那一个无关"
                   "（实测：`selected channelbar_text` 因为 7x24 本来就选中而恒为真）。"
                   "建议改成该控件的**文案**再断状态，或断目标页独有控件"))
    return warns


#: tab 类控件的 res-id 特征（频道栏 / 底部导航 / 分段控件 / 筛选条）。
#: 只用来**认"这一组控件是切换器"**，不是"点了会跳页"——两件事必须分清：
#: 切换器点完**自己还在**（只是换了选中态），跳转类入口点完**自己就没了**。
_TAB_RID_RE = re.compile(r"(?i)(channel|tab|nav|segment|filter|menu|indicator)")


def _tab_rid_of(page: dict, label: str) -> tuple[str, int]:
    """该页上文案为 `label` 的控件：(它的 res-id, 该页同文案的控件个数)。"""
    hits = [c for c in (page.get("controls") or [])
            if str(c.get("text") or c.get("desc") or "").strip() == label]
    rid = str(hits[0].get("res-id") or "").strip() if hits else ""
    return rid, len(hits)


def _labels_of_rid(page: dict, rid: str) -> set[str]:
    """该页上共用这个 rid 的**不同文案**集合（>1 ⇒ 它是"一组控件"而不是单个控件）。"""
    if not rid:
        return set()
    return {str(c.get("text") or c.get("desc") or "").strip()
            for c in (page.get("controls") or [])
            if str(c.get("res-id") or "").strip() == rid
            and str(c.get("text") or c.get("desc") or "").strip()}


def rewrite_coupled_assertions(raw_case: dict, index_dir: Path | str) -> list[str]:
    """**确定性改写**「点完又断言它存在」→ 状态断言（`selected`）。返回 issues（R25）。

    ## 为什么能确定性地改（而不是交给模型/人）

    A2（断言与导航耦合）有两种结局，**图谱能区分**：

    | 结局 | 判据（图谱） | 处置 |
    |---|---|---|
    | 控件**还在**落点页 | `_page_has(after, 断言目标)` 为真 | 断言**恒真**（页面没切也绿）⇒ 改成 `selected`，它才反映"切没切" |
    | 控件**不在**了 | 同上为假 | 断言**必失败** ⇒ 该断"到了哪一页"，**这里不改**（见下） |

    ## 为什么只改 tab 类，不改"到达页"

    第二行的正确处置是"改断落点页的独有控件"。**试过，不能用**：落点页在图谱里绝大多数是
    **同一 activity 的碎片**（信息流换频道），能挑出来的"独有控件"全是**文章标题**
    —— 换成它等于把一个必失败换成另一个必失败（内容快照，R4）。
    实测（2026-10-24，tx-news 10 条 R2 命中）：能推出落点页的只有 3 条，其中 1 条落点页只有
    3 个控件（迷你播放器），断它只证明"迷你播放器在"，不证明到了早晚报页。
    ⇒ **到达页断言需要图谱里有那个页**，这是图谱覆盖率问题，不是改写规则问题——图谱没探索到入口页时改写帮不上。

    ## 四条护栏（缺一条就不改，只让 R2 去报）

    1. 落点页**已知**（走链断了就弃权 —— 不知道点完在哪，就不知道控件还在不在）；
    2. 控件**还在**落点页；
    3. 文案在本页**唯一**（`text=视频` 同时命中频道栏和底栏时，`selected` 会断到错的那个）；
    4. 它是 **tab 组的一员**：res-id 带 channel/tab/nav 特征，且同 rid 在本页对应 ≥2 个文案；
       且落点页**也有这组 tab**（切换器跟着页面走）——否则它可能是"点完跳走了"的入口。

    ⚠️ 改完的断言依赖**被测 App 暴露 `selected` 状态**。真机验证过（tx-news 8.0.51：
    `channelbar_text` / `nav_btn_text` 都带 `selected`，且只有当前 tab 为 true）；
    App 不暴露时运行期会如实报「节点无 selected 状态」，不会静默变绿。
    """
    pkg = ((raw_case.get("target") or {}) or {}).get("package") or ""
    if not pkg:
        return []
    try:
        case = TestCase.model_validate(raw_case)
    except Exception:      # noqa: BLE001 - 结构不合法的输入交给 review/审计去报
        return []
    index = load_index(Path(index_dir) / f"{pkg}.json")
    pages = [p for p in (index or {}).get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    if not pages:
        return []
    raw_steps = {s.get("id"): s for s in raw_case.get("steps", []) if isinstance(s, dict)}
    issues: list[str] = []

    for st, (before, after) in zip(case.steps,
                                   _walk_expected_pages(case, pages, (index or {}).get("edges") or []),
                                   strict=False):
        if st.action not in _FORWARD_ACTIONS or st.target is None or before is None or after is None:
            continue
        if str(getattr(st.target, "strategy", "")) not in _TEXT_LIKE:
            continue
        value = (st.target.value or "").strip()
        if not value or _is_noise_tap(value):
            continue
        rid, same_text = _tab_rid_of(before, value)
        if same_text != 1 or not _TAB_RID_RE.search(rid):
            continue
        if len(_labels_of_rid(before, rid)) < 2 or len(_labels_of_rid(after, rid)) < 2:
            continue
        raw_step = raw_steps.get(st.id)
        if raw_step is None:
            continue
        for assertion in (raw_step.get("assertions") or []):
            if not isinstance(assertion, dict):
                continue
            at = assertion.get("target")
            atype = str(assertion.get("type") or "")
            if not isinstance(at, dict) or atype in STATE_ASSERTION_TYPES or atype == "not-exists":
                continue
            if (str(at.get("value") or "").strip() != value
                    or str(at.get("strategy") or "") != str(st.target.strategy)):
                continue
            if not _page_has(after, st.target):
                continue          # 真跳转：控件不在了 ⇒ 该断到达页，`selected` 会恒假
            assertion["type"] = "selected"
            assertion.pop("expected", None)
            issues.append(fmt_issue(
                "R25", f"步骤 {st.id}：`{st.action.value}({value})` 之后又断言它「存在」"
                       f"（它在落点页仍然存在 ⇒ 恒真，页面没切也照样绿），"
                       f"已自动改为 `selected`——现在只有**真的切过去**才会通过"))
    return issues


def index_rewrites(raw_case: dict, index_dir: Path | str) -> list[str]:
    """**要图谱的确定性改写**的唯一入口（顺序固定，别在调用方各挑一个）。

    ① `sanitize_ambiguous_rids`（R21）：共用 res-id → 唯一文案；
    ② `rewrite_coupled_assertions`（R25）：导航耦合断言 → 状态断言。

    顺序不能反：② 的护栏之一是"文案在本页唯一"，而 ① 正是把共用 rid 换成唯一文案的那一步。
    """
    return (sanitize_ambiguous_rids(raw_case, index_dir)
            + rewrite_coupled_assertions(raw_case, index_dir))


def sanitize_ambiguous_rids(raw_case: dict, index_dir: Path | str) -> list[str]:
    """**确定性改正**「共用 res-id」：当该页有多个控件共用同一 rid、而本步意图只指向其中一个时，
    把定位改成那条文案。返回 issues（改了就报）。

    真机背景（2026-10-21，tx-news 4 条频道切换用例）：7 个频道 tab（7x24/要闻/关注/视频/天津/
    国际/娱乐）**共用 `res-id=channelbar_text`**，用例写 `res-id=channelbar_text` 就永远点第一个 tab
    ——每一步都报成功，但测的不是想测的频道。而用例自己**知道**要哪个：
    `intent="点击首页的要闻频道栏"`、`params={"text": "要闻"}`。

    只在**意图里恰好命中一条**时才改（命中 0 条或多条一律不动，只报 R21）——宁可不改，不可改错。
    """
    pkg = ((raw_case.get("target") or {}) or {}).get("package") or ""
    if not pkg:
        return []
    try:
        case = TestCase.model_validate(raw_case)
    except Exception:      # noqa: BLE001 - 结构不合法的输入交给 review/审计去报
        return []
    index = load_index(Path(index_dir) / f"{pkg}.json")
    pages = [p for p in (index or {}).get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    if not pages:
        return []
    extra_edges = (index or {}).get("edges") or []
    raw_steps = {s.get("id"): s for s in raw_case.get("steps", []) if isinstance(s, dict)}

    issues: list[str] = []
    for st, (before, _after) in zip(case.steps, _walk_expected_pages(case, pages, extra_edges),
                                     strict=False):
        if st.target is None or before is None:
            continue
        if str(getattr(st.target, "strategy", "")) not in _ID_LIKE:
            continue
        if getattr(st.target, "index", None) is not None:
            # 作者已用 `index` 显式指定"第几个" ⇒ 共用 res-id 是**有意为之**（如
            # `channelbar_text + index=1`）。此时按意图改写成某条文案会把「位置型意图」
            # 变成「内容型意图」——对信息流卡片恰恰是**更脆**的方向（内容每天都变），
            # 与 R21 原本要解决的问题相反，所以放行不改。
            continue
        value = (st.target.value or "").strip()
        if not value or _is_noise_tap(value):
            continue
        group = _equiv_group(before, pages)
        if max((_rid_match_count(pg, value) for pg in group), default=0) <= 1:
            continue
        texts = list(dict.fromkeys(
            str(c.get("text") or c.get("desc") or "").strip()
            for pg in group for c in (pg.get("controls") or [])
            if str(c.get("res-id") or "").strip() == value
            and str(c.get("text") or c.get("desc") or "").strip()))
        hint = " ".join([str(getattr(st, "intent", "") or ""),
                         str((getattr(st, "params", None) or {}).get("text") or ""),
                         " ".join(str(a.expected or "") for a in st.assertions)])
        matched = [tx for tx in texts if tx and tx in hint]
        if len(matched) != 1:
            continue                      # 意图不唯一 ⇒ 不动，只让 R21 去报
        raw_step = raw_steps.get(st.id)
        if raw_step is None:
            continue
        raw_step["target"] = {"strategy": "text", "value": matched[0]}
        issues.append(fmt_issue(
            "R21", f"步骤 {st.id}：res-id「{value}」在该页有多个控件共用，"
                   f"已按本步意图自动改为 text「{matched[0]}」"
                   "（否则永远命中第一个，点到的不是想要的那个）"))
    return issues


def _cross_page_issues(case: TestCase, index_dir: Path, pkg: str) -> list[str]:
    """R20：定位值在**全图谱有**、但**本步所在页（含同逻辑碎片）没有**（只报）。"""
    return [fmt_issue(
        "R20", f"{c['label']}：定位「{c['value']}」在**本步所在页（{c['page']}）**不存在"
               "（图谱其他页有）——大概率点完落到了别的页/同逻辑页的另一个碎片，"
               "运行期会定位失败；建议改用该页实际存在的控件，或把导航步骤拆开")
        for c in page_conflicts(case, index_dir, pkg)]


def page_guard_coverage(case: TestCase, index_dir: Path | str, pkg: str) -> tuple[int, int]:
    """页面一致性闸门的**可判率**：`(可判步数, 有定位的步数)`。

    为什么必须报这个数（2026-10-21 实测教训）：闸门与 R20 共用"沿图谱走链"的判据，
    走链一断就**弃权**（宁可漏报不可误报）。于是它在老图谱上可能**一条都不报**——
    某生活服务 App 3 条真实失败用例上冲突数全是 0，不是用例没问题，而是走链在第一步就断了
    （某生活服务 App `parent_rid` 全为空，生成用例却大量用 res-id 点击）。
    不报这个数，读者会把"没报错"当成"检查通过"。
    """
    index = load_index(Path(index_dir) / f"{pkg}.json")
    pages = [p for p in (index or {}).get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    if not pages:
        return 0, 0
    extra_edges = (index or {}).get("edges") or []
    judged = total = 0
    for st, (before, after) in zip(case.steps, _walk_expected_pages(case, pages, extra_edges),
                                     strict=False):
        if st.target is None and not st.assertions:
            continue
        if st.target is not None and _is_noise_tap(st.target.value or ""):
            continue          # 噪声步骤不参与页面校验，也不该占"可判率"的名额
        total += 1
        page = before if st.target is not None else (after if after is not None else before)
        if page is not None:
            judged += 1
    return judged, total


def page_guard_feedback(case: TestCase, index_dir: Path | str, pkg: str,
                        limit: int = 4) -> str:
    """生成期**页面一致性**反馈（给模型看）；无冲突返回空串。

    由 `ai_client` 在内容级重试回路里调用：有冲突就抛 `GenError`，把这段文字回喂给模型
    重写。**为什么必须在生成期做**：R20 只在跑测后报 WARN，用例照样落盘、照样上真机
    ——2026-10-21 实测 3 条同代用例全挂在"断言写的是上一页的控件"上。
    """
    conflicts = page_conflicts(case, index_dir, pkg)
    if not conflicts:
        return ""
    lines = ["上一次输出里，这些定位**不属于它所在的那一页**（真机运行必然定位失败）："]
    for c in conflicts[:limit]:
        cand = "、".join(c["candidates"]) or "（该页没有可用控件，请改用该页存在的控件或删掉该步断言）"
        lines.append(f"- {c['label']}：`{c['value']}` 不在「{c['page']}」上。"
                     f"「{c['page']}」上可用的有：{cand}")
    if len(conflicts) > limit:
        lines.append(f"- （另有 {len(conflicts) - limit} 处同类问题未列出）")
    lines.append("请只从上面列出的候选里选（res-id 或文案），改正后**重新输出完整 JSON**。")
    return "\n".join(lines)


def case_quality_edges(pages: list[dict], extra_edges: list[dict] | None = None) -> list[dict]:
    """从页面记录里还原导航边（`parent_key + parent_action` → `{from, to, action}`）。

    图谱顶层 `edges` 只覆盖 explore 探到的跳转（某生活服务 App 111 条 / 177 页），而
    `parent_key/parent_action` 覆盖更多（168 条）——两者合并取并集，走链更不容易断。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for e in extra_edges or []:          # 图谱顶层 edges（explore 探到的真实跳转）
        src, dst = e.get("from"), e.get("to")
        act = (e.get("action") or "").strip()
        rid = (e.get("rid") or "").strip()
        if src and dst and (act or rid):
            key = (src, act or rid, dst)
            if key not in seen:
                seen.add(key)
                out.append({"from": src, "to": dst, "action": act, "rid": rid})
    for p in pages:
        parent = p.get("parent_key")
        action = (p.get("parent_action") or "").strip()
        rid = (p.get("parent_rid") or "").strip()
        if parent and (action or rid) and p.get("page_key"):
            key = (parent, action or rid, p["page_key"])
            if key not in seen:
                seen.add(key)
                out.append({"from": parent, "to": p["page_key"], "action": action, "rid": rid})
    return out
