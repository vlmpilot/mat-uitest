"""页面索引 / 页面图谱（本地实现）。

两层职责：
1. **控件索引**（原有）：真机轻量爬取 → 控件清单 → 注入生成/转换/归因。
   消除 AI 幻觉的底座：gen/convert 只允许引用索引中真实存在的控件（02 §3.4）。
   防误点黑名单：支付/结算/客服/电话等敏感动作在爬取期不可点击（爬取是探测，不是交易）。
2. **页面图谱**（新增）：把 explore 花真机预算换来的层级信息
   （depth / parent_key / parent_action / tried / alt_entries / step_health）
   沉淀进 `artifacts/page_index/<pkg>.json`，供**下次探索增量复用**与**执行期归因**。

**关键兼容性决策**：`pages` **始终是 list**，不改成以 page_key 为键的 dict。
理由：`controls_context(index.get("pages", []))` 被 `__main__.py` 与 `web_server.py` 两处按
list 消费；改 dict 会同时打断两处。图谱新增字段一律**纯增量**，旧消费者零改动。

**动态文本过滤（`dynamic_regions` 思路，正则轻量版）**：
`page_key` 是控件语义集合的哈希；时间戳、"3 条未读"、价格、相对时间等**动态文本**会让
**同一页面**产生不同 key → 图谱膨胀。算 key 前把明显动态的
text/desc **置空**（保留 res-id，因为 res-id 通常是稳定的），长文本（多为内容而非结构）同样置空。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from .safety import SENSITIVE_WORDS as _SENSITIVE  # 单一事实来源（safety.py）
from .safety import is_ad_slot_rid, is_splash_skip_label
from .ui_tree import parse_nodes


def _pause(seconds: float) -> None:
    time.sleep(seconds)

SCHEMA_VERSION = 2

# -- 动态文本识别（page_key 抗膨胀） ---------------------------------------------

# 只过滤「明显是动态」的整串 token，宁可漏杀不可错杀：错杀会让两个不同页面撞 key。
_DYNAMIC_PATTERNS = (
    re.compile(r"^\d+$"),                                                    # "3" "12"
    re.compile(r"^\d[\d,. ]*%?$"),                                           # "1,234" "12.5" "50%"
    re.compile(r"^\d+(?:\.\d+)?\s*(?:条|个|人|次|天|小时|分钟|秒|米|m|km|kg|g|元|万|千|亿|k|w|℃)\+?$",
               re.I),                                                        # "581条" "8千+" "649m"
    re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$"),                               # "12:30"
    re.compile(r"^\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?$"),                # "2026-09-28"
    re.compile(r"^(?:刚刚|昨天|前天)$"),
    # **「标签 + 计数」**（2026-10-22，某内容社区 App实测）：`点赞 1336` / `收藏135` / `评论 409`
    # 带的是会变的数字，但整体既不是纯数字、也短于 40 字符 ⇒ 旧规则全放过。
    # 后果：同一个笔记详情页每次进来计数不同 ⇒ `page_key` 每帧都变 ⇒ 回退落点被当成新页，
    # 探索"每帧一个新页"，26 页里 16 页是同一个页面反复新建，最后 `stuck`。
    # ⚠️ 必须要求**带数字**：`收藏`/`评论`/`关注` 这种纯标签是稳定入口，不能抹。
    re.compile(r"^(?:点赞|收藏|评论|赞|阅读|浏览|分享|转发|播放|在看|喜欢|粉丝|关注|浏览)"
               r"\s*[\d.,]+\s*(?:万|千|亿|w|k)?\+?$", re.IGNORECASE),
    # 图片位置指示 `1/6`：同一个详情页每张图都不同，但对"这是哪一页"没有信息量。
    re.compile(r"^\d+\s*/\s*\d+$"),                                      # 相对时间
    re.compile(r"^\d+\s*(?:分钟|小时|天|周|个?月|年)前$"),                     # "3 分钟前"
    # **状态指示文案**（开关/连接/安装状态）。它们不随内容变，而是**随设备状态变**——
    # 但对页面身份而言同样是噪声：同一页只是某个开关被拨了一下，就会被算成两个页面。
    #
    # 为什么必须覆盖（根因，2026-10-02 实测）：探索**自己会拨开关**
    # （点了设置页的蓝牙开关）⇒ 页面文案「已关闭」变「已开启」⇒ `page_key` 变化 ⇒
    # 下一轮从"不同页"开始、结果只有上一轮的 1/3。三轮实测：
    # `[13步/11页, 5步/4页, 13步/11页]`，L0 的 key 在 `3c6d…` 与 `05f7…` 之间**周期 2 振荡**
    # ——**探索方差的主要来源不是随机抖动，是它自己的副作用**。
    #
    # 只匹配**整串就是状态词**（`^…$`），所以「蓝牙 已关闭」里的「蓝牙」仍参与签名，
    # 页面不会被抹平。保守起见**不收**裸「开」「关」——它们太短，可能是别的标签的一部分。
    re.compile(r"^(?:已|未)?(?:开启|关闭|连接|断开|启用|停用|禁用|激活|安装|更新|选中|勾选|打开)$"),
)

_MAX_TEXT_LEN = 40  # ≥ 此长度的文本按「内容」处理（商品名/描述），不参与页面签名

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def is_dynamic_text(text: str, max_len: int = _MAX_TEXT_LEN) -> bool:
    """文本是否「会随时间/内容变化」——动态文本不参与页面签名，否则同页会产生多个 key。"""
    text = (text or "").strip()
    if not text:
        return False
    if len(text) >= max_len:
        return True
    return any(pattern.match(text) for pattern in _DYNAMIC_PATTERNS)


#: 「**部分动态**」文案：固定文案 + 会变的计数（`消息，1条未读`、`3人想买`）。
#:
#: ⚠️ **不并入 `is_dynamic_text`**。那条判据是给 `page_key` 用的，注释写明"宁可漏杀不可错杀
#: ——错杀会让两个不同页面撞 key"；这里如果也按"整串匹配"放宽，`3人成团` 这类稳定文案会被
#: 误杀。所以只用于**控件清单 / 候选集**两个出口：少一条候选无害，而让模型照抄
#: `text=消息，1条未读` 有害——未读数一变（1→3）就定位失败，且这类失败会被误归因成"文案过期"。
#:
#: 实测来源（2026-10-28）：xhs 底部导航的「消息」tab 只有 `content-desc='消息，1条未读'`
#: （可点容器）+ `text='消息'`（不可点 TextView）。放开退化 rid 之后前者进了候选集，
#: 模型立刻照抄 ⇒ 3/3 都写死未读数。
_DYNAMIC_SEGMENT_RE = re.compile(
    r"\d+\s*(?:条|个|人|次|天|小时|分钟|秒|元|万|千|亿|w|k|%)", re.IGNORECASE)


def has_dynamic_segment(text: str) -> bool:
    """文案里是否**夹着**会变的计数（见 `_DYNAMIC_SEGMENT_RE`）。只用于候选集过滤。"""
    return bool(_DYNAMIC_SEGMENT_RE.search(text or ""))


def stable_prefix(label: str) -> str:
    """`消息，1条未读` → `消息`：取动态段**之前**的固定部分（去掉尾部标点）。

    ## 为什么不是"整条丢掉"（2026-10-28 实测踩到）

    第一版把带计数的文案整条过滤掉，结果 xhs「消息页」这条意图**从错法 A 换成错法 B**：

    | 候选集 | 模型写的定位 | 后果 |
    |---|---|---|
    | 旧（没有底部导航） | `text=消息` | **对的**，但 R18 报"不在清单" |
    | 放开退化 rid | `text=消息，1条未读` | 未读数一变就定位失败 |
    | 再加"整条过滤" | `text=我` | **点错页**（清单里只剩别的 tab） |

    根因：`消息` 这个文案**真机确实存在**（不可点的 TextView，`text='消息'`），
    只是没有进清单 ⇒ 模型只能在清单里挑，挑了个错的。
    ⇒ 正确做法是取稳定前缀，并且**要求它在该页真的有 `text`**（见调用方），否则宁可不收。
    """
    m = _DYNAMIC_SEGMENT_RE.search(label or "")
    if not m:
        return (label or "").strip()
    # 去尾部标点：**按字符集合**剥（`，`/`,`/`、`/`:`/`·`/`|`/`/`/`-`/破折号/空格 任一）。
    # 这里就是想要集合语义，不是想要"这一整串"，故显式关掉 B005。
    return (label or "")[:m.start()].rstrip("，,、:：·|/-—  ")  # noqa: B005


def _stable_text(text: str) -> str:
    """动态文本置空（保留控件本身，其 res-id 仍可作稳定标识）。"""
    return "" if is_dynamic_text(text) else text


def _dedup_seq(values: list) -> list:
    """保序去重。"""
    seen: set = set()
    out: list = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


#: 「**退化 res-id**」——**零区分度**的资源名，不能当页面信号。
#:
#: 实测（2026-10-07，T71）：某 App 的 **143/144 页**都含
#: `rid:com.xingin.xhs:id/0_resource_name_obfuscated`。它是 aapt2 在**资源名解析不出来时
#: 的占位名**——同一页面上多个不同控件都会叫这个。
#:
#: **它造成的危害是「指纹太弱」，不是「页数膨胀」**（⚠️ 我一开始写反了，实测纠正）：
#: `identities_from_controls` 里 **rid 优先且无条件加入**（`if rid: add; continue`）
#: ⇒ 带占位 rid 的控件**再也不会贡献它的文案** ⇒ 121 个 GlobalSearch 页里
#: **41 页的信号集完全相同**（都只剩那个占位 rid）。
#:
#: 修好之后（用图谱里存的 `controls` 离线重算）：信号集种数 **77 → 135**、
#: 最大重复 **41 → 1** ⇒ **那些页本来就是不同的页**（搜索词不同），
#: 是占位名**把差异盖掉了**。
#:
#: ⇒ 收益有两条：① 信号集不再被抹平；② 包含度认页的**跳过率 80.4% → 85.1%**、
#: **跨页误判 0.5% → 0.3%**（语料实测）。⚠️ **它不解决"页数膨胀"**——
#: 那个来自**内容驱动的 `page_key` 变化**（同一功能页 + 不同内容 ⇒ 不同 key）。
#:
#: 判据是**名字以数字开头**，不是硬编码某个字符串：Android 资源名必须是合法 Java 标识符
#: （**不能以数字开头**）⇒ 以数字开头的一律是 aapt2 的占位。
#: 实测全部 4 个 App 的图谱里，满足这条的**只有** `0_resource_name_obfuscated` 一个。
#: `tried` 条目的格式：`<bounds>\t<label>\t<rid>`。老格式（裸 bounds）没有分隔符 ⇒ 视为"不可验证"。
#: 为什么用 \t：a11y 文案里几乎不出现制表符，且 JSON 里可见、好排查。
TRIED_SEP = "\t"


def _clean(value: object) -> str:
    return str(value or "").replace(TRIED_SEP, " ").strip()


#: 语义比较前把**数字段归一化**成 `#`。
#: 为什么不是"含数字就丢掉"（2026-10-22 实测后改）：丢掉会让"收藏按钮"这类控件退化成
#: 只比坐标——而坐标在信息流里恰恰不可靠，等于把刚修好的**误跳**又放回来。
#: 归一化两头都占：同一个控件（计数变了）仍相等，不同内容（标题不同）仍不等。
#: 实测某内容社区 App："已收藏10004" / "消息，1条未读" / "5475" / "1万" 都是同一个控件的不同计数。
_DIGIT_RUN_RE = re.compile(r"\d+")


def _semantic_label(cand: dict) -> str:
    """可当"控件身份"的文案：数字段归一化为 `#`（见 `_DIGIT_RUN_RE`）。"""
    return _DIGIT_RUN_RE.sub("#", _clean(cand.get("label")))


def tried_entry(cand: dict) -> str:
    """候选 → `tried` 条目：`bounds \t label \t rid`。

    为什么三个都记（2026-10-22 实测）：原来只记 `(page_key, bounds)`，等于**用屏幕坐标
    当"这个分支是什么"**。信息流/列表页的卡片按坐标重排 ⇒ 同一坐标每次是不同内容 ⇒
    旧条目**误跳新入口**（静默漏页）。但单靠任一个语义字段也不够稳：

    | 字段 | 什么时候稳 | 什么时候不可靠 |
    |---|---|---|
    | `label` | 按钮/页签文案 | 图标入口没有文案 |
    | `rid`   | 原生 App | RN/混淆包（`0_resource_name_obfuscated`）或每次变（实测部分 App 如此）|
    | `bounds`| 固定布局 | 信息流/长列表 |

    ⇒ 三个都记，**匹配时要求 bounds 相同、且 label / rid 至少有一个相同**（空字段不算）。
    """
    return TRIED_SEP.join((_clean(cand.get("bounds")), _semantic_label(cand),
                           _clean(cand.get("rid"))))


def tried_matches(entry: str, cand: dict) -> bool:
    """历史条目是否**就是**这个候选。

    - 老格式（没有分隔符）⇒ **不匹配**：不可验证，宁可不跳（重探一次）也不误跳（静默漏页）；
    - 新格式 ⇒ bounds 必须相同，且 label / rid **至少一个相同**（两者都空则只比 bounds）。
    """
    parts = str(entry).split(TRIED_SEP)
    if len(parts) < 3:
        return False
    old_bounds, old_label, old_rid = parts[0], parts[1], parts[2]
    if old_bounds != _clean(cand.get("bounds")):
        return False
    # ⚠️ **退化 rid 不参与比较**：`0_resource_name_obfuscated` 这类占位名在**所有**控件上
    # 都一样，拿它判"同一个控件"等于没判（实测某内容社区 App全 App 共用一个占位名）。
    now_rid = _clean(cand.get("rid"))
    if is_degenerate_res_id(old_rid):
        old_rid = ""
    if is_degenerate_res_id(now_rid):
        now_rid = ""
    # 存量条目里存的是**未归一化**的原文案，读的时候也要归一化一次（幂等）
    label_ok = bool(old_label) and _DIGIT_RUN_RE.sub("#", old_label) == _semantic_label(cand)
    rid_ok = bool(old_rid) and old_rid == now_rid
    if not old_label and not old_rid:
        return True          # 两个语义字段都空（罕见）：只能退回比坐标
    return label_ok or rid_ok


def is_legacy_tried(entry: str) -> bool:
    """老格式条目（没有语义段）——读回图谱时丢弃并计数。"""
    return str(entry).count(TRIED_SEP) < 2


def is_degenerate_res_id(res_id: str) -> bool:
    """资源名是不是「占位/无区分度」的那种（只按**名字**判，不看包名）。"""
    name = (res_id or "").rsplit("/", 1)[-1].strip()
    return bool(name) and name[0].isdigit()


#: 稳定信号少于这个数时，补一层**结构信号**（见 `identities_from_controls`）。
STRUCTURAL_MIN_SIGNALS = 3


def identities_from_controls(controls: list[dict]) -> frozenset[str]:
    """从控件摘要（digest_page 产物）构建页面信号集合——与 explore._identities 同语义。

    为什么在 page_index 而不是 explore：`mat index`（crawl）落盘的页面只有 controls、
    没有 identities，导致**首次使用主路径**（mat index → mat explore）上包含度认页
    静默失效（外部评审 P0-3）。归一化阶段就地零成本补算，两条建图路自动等价。
    语义对齐由 test_explore_incremental 的"两实现一致性"断言守护。

    ⚠️ **退化 res-id 要跳过、并回落到 text/desc**（T71）：否则占位名会进指纹
    （见 `is_degenerate_res_id`）。跳过而不是"整个控件丢掉"——那个控件的文案往往
    仍有区分度（那些页除了占位 rid，还带着各自的搜索词）。
    """
    signals: set[str] = set()
    for c in controls or []:
        rid = c.get("res-id", "")
        if rid and not is_degenerate_res_id(rid):
            signals.add(f"rid:{rid}")
            continue
        for prefix, value in (("text:", c.get("text")), ("desc:", c.get("desc"))):
            if value and not is_dynamic_text(value):
                signals.add(prefix + value)
    # **结构性兜底**（2026-10-22，某内容社区 App实测）：内容页（搜索结果/信息流/详情）的文案
    # 全是动态的、res-id 又全混淆 ⇒ 稳定信号可能只剩 1~3 个，包含度认页直接失效：
    # 153 页里 **17 页只有 1 个信号**，两两近重复对 1112 组 —— 探索于是"每帧一个新页"，
    # 深度爆炸后 `stuck`。此时补一层**与内容无关**的结构信号（控件类名 + 是否可点），
    # 让"同一个页面类型"仍然认得出彼此。
    # ⚠️ 只在信号**稀薄时**才补（阈值 3）：文案充足的页面不该被结构信号稀释，
    # 否则「设置」这类同布局不同标题的页会被误并。
    if len(signals) < STRUCTURAL_MIN_SIGNALS:
        for c in controls or []:
            cls = str(c.get("class") or "").rsplit(".", 1)[-1].strip()
            if cls:
                signals.add(f"cls:{cls}:{'1' if c.get('clickable') else '0'}")
    return frozenset(signals)


#: 判定「同逻辑页碎片」的 identities Jaccard 门槛（实测同页稳定性仅 12%，
#: 同一逻辑页常碎片化成多个 page_key；0.5 在实测样例 J≥0.76 的真碎片与凑巧
#: 同 activity 的无关页之间留了余量）。
EQUIV_SIM = 0.5


def _ident_jaccard(a: dict, b: dict) -> float:
    sa = {str(s) for s in (a.get("identities") or [])}
    sb = {str(s) for s in (b.get("identities") or [])}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def pages_equivalent(a: dict, b: dict, min_sim: float = EQUIV_SIM) -> bool:
    """两页是否为**同一逻辑页的碎片**：同 activity 且 identities Jaccard ≥ 阈值。

    用途：落点核对/状态核对不再按 page_key 精确相等——内容轮换/弹窗变体产生的
    碎片会让精确核对把「实际已到达」误判为「落点不符」（走链重放与执行期置备
    共用这一判据，见 arrivals._walk_chain）。identities 任一为空或 activity 不同
    ⇒ 不等价（宁可判不符，不可把真不同页放进来）。
    """
    if not a or not b:
        return False
    if (a.get("activity") or "") != (b.get("activity") or ""):
        return False
    return _ident_jaccard(a, b) >= min_sim


#: iconfont 私有区字符（Android 图标字体常落在 BMP PUA 与两个补充私有区）。
_PUA_RE = re.compile(r"^[\s\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]+$")


def is_pua_label(label: str) -> bool:
    """文案是否纯 iconfont 私有区字符（如 ``\\ue90f``）——重放按文案匹配不到语义。

    实测（第 4 层，tx-news）：explore 把图标按钮的 iconfont 字符记成 parent_action，
    真机重放按「文案」逐跳匹配必死——字符看着非空，实际没有控件语义。
    混合了正常文字的（如「IP属地：云南+图标」）仍有文案语义，不算。
    """
    s = str(label or "").strip()
    return bool(s) and bool(_PUA_RE.match(s))


#: 内容型文案的最短长度：**无 rid** 纯文案跳、文案 ≥ 该长度判「内容型」——
#: 推荐流标题/榜单名等随内容轮换的文本，重放按文案匹配必挂（tx-news 2026-10-17
#: 真机实测：8 条内容文案链 24 轮 0 通过，失败文案 7~24 字全 ≥7；tab/菜单等稳定
#: 短文案 2~4 字不受影响）。宁漏勿错：误伤罕见的 ≥7 字稳定菜单项（少验一条链）
#: 好过把必挂链当可靠（每条白烧 3 轮冷启动真机预算）。
CONTENT_LABEL_MIN = 7


def is_content_label(label: str) -> bool:
    """文案是否内容型（随内容轮换、重放必挂）——长度判据见 `CONTENT_LABEL_MIN`。

    只对**无 rid** 的纯文案跳生效：有 rid 走资源 id 通道，文案轮换无所谓。
    `BACK_HOP` 哨兵（15 字符）是确定性系统动作，显式豁免。
    """
    s = str(label or "").strip()
    return bool(s) and s != BACK_HOP and len(s) >= CONTENT_LABEL_MIN


def chrome_signals(controls: list[dict]) -> frozenset[str]:
    """页面的**UI 骨架信号**：与内容无关的那部分控件（tab / 底栏 / 搜索 / 菜单）。

    ## 为什么需要它（2026-10-29，某内容社区 App 真机实测）

    `identities_from_controls` 的 rid 优先通道要求**非退化 rid**。某内容社区 App
    **全图只有一个非退化 rid**，而且是根容器 `index_store`（每个页面都有）——
    于是 16 个页面（首页 / 市集 / 消息 / 我的）的 rid 集合**完全相同**，
    任何基于 rid 的相似度都恒为 1.0。`fragment_merge` 在这种图谱上会把
    「首页 + 消息 + 我的」并成一页（实测 52 页 → 8 页，85% 是错的）。

    骨架信号从**文案**里取，但只取「不会随内容变」的那一类：

    - 有非退化 rid ⇒ 收 `rid:`（与 identities 同口径，骨架里它也是稳定标识）；
    - 否则看 `desc`/`text`：带计数段的先取稳定前缀（`消息，3条未读` → `消息`）；
      动态整串（`317`、`12:30`）与 iconfont 私有区字符**丢掉**；
    - `class` 名含 `Tab` 的一律收（tab 文案是页面的骨架，与长度无关）；
    - 其余只收 **< `CONTENT_LABEL_MIN`(7) 字**的短文案——推荐流标题/商品名
      都在 7 字以上，而 `搜索`/`菜单`/`关注`/`返回` 这类导航文案在 2~4 字。

    ## 标定（同一批真机图谱，2026-10-29）

    | App / activity | 同页型碎片对 | 跨页型对 |
    |---|---|---|
    | 内容社区 App 首页（3 碎片） | **0.64 ~ 0.74** | ≤ 0.32 |
    | 内容社区 App 详情页（12 碎片） | 中位 1.00 | — |
    | 资讯类启动页（27 碎片） | 0.03 ~ 0.98 | — |

    ⇒ 阈值 0.6 落在「首页碎片 0.64」与「跨页型 0.32」之间，两边都有余量。
    ⚠️ 0.5 会把两个**不同类目**的市集页（运动 / 运动装备，0.55）并掉，故取 0.6。

    纯函数：只看传入的控件摘要，不读盘、不看环境变量。
    """
    out: set[str] = set()
    for c in controls or []:
        rid = str(c.get("res-id") or "")
        if rid and not is_degenerate_res_id(rid):
            out.add(f"rid:{rid}")
            continue
        cls = str(c.get("class") or "").rsplit(".", 1)[-1]
        label = str(c.get("desc") or c.get("text") or "").strip()
        if has_dynamic_segment(label):
            label = stable_prefix(label)
        if not label or is_dynamic_text(label) or is_pua_label(label):
            continue
        if "Tab" in cls:
            out.add(f"tab:{label}")
        elif len(label) < CONTENT_LABEL_MIN:
            out.add(f"txt:{label}")
    return frozenset(out)


#: 系统 back 到达链的**专用标记**（explore 记录 / arrivals 重放 / repair 放行三方共用）。
#:
#: explore 回退恢复时 `press("back")` 可能落到**没爬过的页**（关弹窗露出底层页、
#: 回到未收录的列表页）——此前只能记 `parent_action=None`（结构性断链，tx-news
#: 实测 62/75 新页无链的主因之二）。back 是确定性动作，**可重放**：走到 back 前
#: 的页再按同一动作回放。标记串取不可能与真实控件文案冲突的哨兵值；
#: `is_back_word` 是全等匹配不会误中它，故 repair/verify 的回退过滤自动放行。
BACK_HOP = "__system_back__"


def hop_replayable(action: str | None, rid: str | None) -> bool:
    """单个到达动作是否有**可重放通道**：资源 id，或非 iconfont、非内容型的真实文案。

    explore（`_record` 换链判据）、合并（`_merge_page`）、修复（graph_repair 的
    语义校验）三处共用，避免口径漂移——iconfont 字符看着非空，同样写不出重放序列；
    内容型长文案（`is_content_label`）看着有语义，重放时内容早已轮换，同理必挂。
    """
    if rid:
        return True
    return bool(action) and not is_pua_label(action) and not is_content_label(action)


#: 广告落地页的 activity 标记（**窄判据**：只认 activity 名，不看文案）。
#: 为什么需要它（2026-10-23 实测 tx-news 图谱）：53 条记录里 **15 条**是
#: `.tad.business.ui.activity.WebAdvertActivityV2`——点信息流广告进去的落地页
#: （「商品详情页」「淘宝精选」）。它们不是被测 App 的功能页，却会被当成"可途经的页面"
#: 写进父链：`graph-repair` 实测借到的链，到达动作就是 `天猫双11热卖中`——**重放必死**。
#: ⚠️ 判据刻意**只看 activity**：新闻标题里带「广告」二字很常见，按文案判会误杀真页面。
_AD_ACTIVITY_MARKERS = ("webadvert", "advertactivity", "adactivity", ".ad.")


def is_ad_landing(page: dict) -> bool:
    """这页是不是**广告落地页**（不是被测 App 的功能页）。

    用途：路由（`arrivals._chain_of`）不把它当可途经节点，修复器（`graph_repair`）
    不把它当修复目标、也不拿它的链当 donor。**不删页**——证据留在图谱里可审。
    """
    activity = str(page.get("activity") or "").lower()
    return bool(activity) and any(m in activity for m in _AD_ACTIVITY_MARKERS)


def is_transient_frame(page: dict) -> bool:
    """根页是否「瞬态启动帧」：控件含「跳过」类一次性控件（splash/开屏广告）。

    词表见 `safety.is_splash_skip_label`。只对**链根**（depth-0）判：launch 后该帧
    被 App 自动翻过，其下父链的第 1 跳是对「帧还活着」的竞态，结构性不可重放
    （实测 tx-news：11 个根里只有 2 个含「跳过」词，73-77 控件的 SplashActivity
    首页变体不含——判据在真机语料上零误伤）。
    """
    for c in page.get("controls") or []:
        label = (c.get("text") or c.get("desc") or "").strip()
        if label and is_splash_skip_label(label):
            return True
    return False


def root_reachable(root: dict, launch_activity: str | None) -> bool:
    """链根是否 **launch 可达**：根 activity 与冷启动落点一致（全等判）。

    不可达根的来源：App 停在遗留页（实测：上次会话留下的播客详情页）时建的根，
    activity 与 launch 落点不同——launch 重放永远落不到它，其下链结构性不可重放
    （实测 tx-news：245/252 条可重放链挂这种根，真机验证 0/20 全在根核对处拦下）。
    一边取不到（根无 activity 或图谱未记录 launch_activity）就不表态（"不知道"
    ≠ "不可达"），照旧走链，由真机根核对兜底。

    ⚠️ 教训（2026-10-17 真机实证，勿再「放宽」）：activity 名含 splash/launch
    等词 **≠** 瞬态帧——tx-news 冷启动翻开屏后的稳定落点就是
    `.activity.SplashActivity`，它就是首页 activity（重探两次结果一致）。曾按
    命名词表把这类基准判为「旧口径脏数据」并放宽判据，结果 7 条借到
    HippyActivity 首页根的链 3 轮全灭在根核对（21 次冷启动白烧）。瞬态帧的
    判定只认控件特征（`is_transient_frame`：含「跳过」类控件），不认 activity 名。
    """
    if not launch_activity:
        return True
    act = (root.get("activity") or "").strip()
    return (not act) or act == launch_activity


def describe_structure(controls: list[dict], max_elements: int = 8,
                       max_dynamic: int = 6) -> dict:
    """从控件清单**零 AI 成本**抽取页面结构。

    这是「功能描述」里**结构化**的那一半；语义描述（"这是登录页"）才需要 AI
    （见 `summary` / `summary_source`）。

    - `layout_regions`：按控件纵向位置分「顶部/中部/底部」三带（几何启发，非模型推断）；
    - `key_elements`：稳定短文案（动态文本与长文本已剔除），自顶向下排列；
    - `dynamic_regions`：被判为动态的文案——**这正是 page_key 脆弱性的根因清单**
      ，显式记录便于人工核对与后续过滤策略调参。
    """
    boxes: list[tuple[dict, int, int]] = []
    for control in controls or []:
        match = _BOUNDS_RE.match(control.get("bounds") or "")
        if not match:
            continue
        x1, y1, x2, y2 = (int(g) for g in match.groups())
        boxes.append((control, y1, (y1 + y2) // 2))

    bands: dict[str, list[str]] = {"顶部": [], "中部": [], "底部": []}
    if boxes:
        top = min(b[1] for b in boxes)
        bottom = max(b[2] for b in boxes)
        span = max(bottom - top, 1)
        for control, _y1, mid in sorted(boxes, key=lambda b: (b[1], b[2])):
            label = (control.get("text") or control.get("desc") or "").strip()
            if not label or is_dynamic_text(label):
                continue
            fraction = (mid - top) / span
            name = "顶部" if fraction < 1 / 3 else ("底部" if fraction >= 2 / 3 else "中部")
            bands[name].append(label)

    layout_regions = [{"name": name, "elements": _dedup_seq(bands[name])[:max_elements]}
                      for name in ("顶部", "中部", "底部") if bands[name]]
    key_elements = _dedup_seq([e for name in ("顶部", "中部", "底部") for e in bands[name]])[:max_elements]

    dynamic: list[str] = []
    for control in controls or []:
        for field in ("text", "desc"):
            value = (control.get(field) or "").strip()
            if value and is_dynamic_text(value):
                dynamic.append(value)
                break

    return {"layout_regions": layout_regions, "key_elements": key_elements,
            "dynamic_regions": _dedup_seq(dynamic)[:max_dynamic]}


# -- 控件索引（原有） -------------------------------------------------------------

def res_evidence(index_path: Path, res_ids: list[str], limit: int = 4) -> dict:
    """批量查一组 res-id 在页面图谱里的**存在性与历史可见文案**（诊断归因用）。

    与 `rid_hints` 同源（rid_hints 就是本函数的单 id 薄包装）：同一份 json 只读一次、
    同一套去重/动态文案过滤/可点击优先排序。返回::

        {"updated_at": "2026-10-14T…" or None,   # 快照时间——引用方必须把它喂给 LLM，
         "hits": {                                # 否则模型无从判断图谱是否已过期
           "<res_id>": {"exists": bool, "labels": ["登录", …], "pages": 2},
         }}

    图谱文件缺失/损坏/包名非法 → `{"updated_at": None, "hits": {}}`（调用方静默降级）。
    """
    empty: dict = {"updated_at": None, "hits": {}}
    try:
        data = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    want = [r for r in res_ids if r]
    if not want:
        return empty
    hits: dict[str, dict] = {r: {"exists": False, "labels": [], "pages": 0} for r in want}
    for page in data.get("pages") or []:
        for control in page.get("controls") or []:
            rid = control.get("res-id")
            if rid not in hits:
                continue
            hit = hits[rid]
            hit["exists"] = True
            hit["pages"] += 1
            label = (control.get("text") or control.get("desc") or "").strip()
            # 去重必须**跨桶**做：同一文案可能同时出现在可点击与不可点击的节点上，
            # 按桶去重会让它在拼接后重复出现（rid_hints 的老教训）。
            if not label or label in hit["labels"] or is_dynamic_text(label):
                continue
            hit["labels"].append(label)
    for hit in hits.values():
        hit["labels"] = hit["labels"][:limit]
    return {"updated_at": data.get("updated_at") or data.get("generated_at"),
            "hits": hits}


def rid_hints(index_path: Path, res_id: str, limit: int = 4) -> list[str]:
    """从页面图谱反查某个 res-id 曾经对应的**可见文案**（供「改 id 后零成本自愈」使用）。

    为什么需要这一条：`res-id` 改名是真实 App 改版**最常见**的形式，而这类失效
    **没有文案线索**——通用本地模糊自愈从定位器推导出的 token 是 id 片段（`login_btn`），
    匹配不到界面文案（"登录"）。图谱里恰好有历史映射（`res-id → text/desc`），
    正好补上这条线索，而且是零成本的。

    返回顺序：**可点击控件的文案优先**（同一 id 若在多页出现，先给最可能是操作目标的那条）。
    """
    return (res_evidence(index_path, [res_id]).get("hits", {}).get(res_id)
            or {}).get("labels", [])[:limit]


def digest_page(xml: str, package: str = "") -> dict:
    """a11y XML → 控件摘要：仅收被测 App 的可交互/有文本节点（过滤 IME 等外部窗口）。"""
    controls: list[dict] = []
    for node in parse_nodes(xml):
        if not node.in_package(package) or not (node.clickable or node.text or node.desc):
            continue
        entry: dict = {"class": node.cls}
        if node.clickable:
            entry["clickable"] = True
        if node.text:
            entry["text"] = node.text
        if node.desc:
            entry["desc"] = node.desc
        if node.rid:
            entry["res-id"] = node.rid
        if node.bounds:
            entry["bounds"] = node.bounds
        # `selected` / `checked` **只在 True 时写**（与 `clickable` 同口径）：False 是绝大多数，
        # 写进去只是让每份图谱膨胀、且"没有这个键"与"值为 False"对消费方语义相同。
        if node.selected:
            entry["selected"] = True
        if node.checked:
            entry["checked"] = True
        controls.append(entry)
    return {"node_count": len(controls), "controls": controls}


def page_key(xml: str, package: str = "") -> str:
    """页面签名：控件语义集合的哈希（排序容忍差异；动态文本先置空以抗膨胀）。"""
    digest = digest_page(xml, package)
    fingerprint = sorted(
        (_stable_text(c.get("text", "")), _stable_text(c.get("desc", "")), c.get("res-id", ""))
        for c in digest["controls"]
    )
    raw = json.dumps(fingerprint, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def page_purpose_lines(pages: list[dict], max_notes: int = 60,
                       title_limit: int = 30, summary_limit: int = 50) -> list[str]:
    """页面用途行（`- 标题: 描述`）——让 AI 生成/转换时**看懂页面职责**的上下文。

    只收**有功能描述**的页（`summary` 由 `mat describe`（AI）或人工复核写入）；
    名称用页面标题，没有标题时退回 Activity 短名。行数限量（max_notes）：
    大图谱（如 151 页）全量注入会让 prompt 膨胀，预算优先给控件清单（防幻觉底座）。
    """
    lines: list[str] = []
    for page in pages or []:
        summary = (page.get("summary") or "").strip()
        if not summary:
            continue
        name = (page.get("title") or "").strip() \
            or (page.get("activity") or "").split(".")[-1] or "（未命名页）"
        lines.append(f"- {name[:title_limit]}: {summary[:summary_limit]}")
        if len(lines) >= max_notes:
            break
    return lines


def _round_robin(groups: list[list[str]], limit: int) -> list[str]:
    """从多组候选里**轮流**取，取满 `limit` 条为止。

    为什么不是"一组取完再取下一组"（2026-10-26）：控件清单是按页分组的，而
    "第一组取完再取下一组"等价于"把 token 预算全给第一个页面"。某资讯类 App 图谱 24 页，
    首页 600 个控件 ⇒ 120 条配额被首页吃光，模型只看到 1 页，却要写"进详情页/搜索结果页"
    的定位值——它没见过那些控件，只能编。轮转不增加预算，只让每一页都露面。
    """
    out: list[str] = []
    depth = 0
    while len(out) < limit:
        added = False
        for group in groups:
            if depth < len(group):
                out.append(group[depth])
                added = True
                if len(out) >= limit:
                    break
        if not added:
            break
        depth += 1
    return out


#: 广度配额占比（2026-10-26 真机标定）。**纯轮转是过冲**——见 `_quota_split`。
BREADTH_SHARE = 0.5

#: 合并行里最多列几个文案（2026-10-28）。某社区类 App 有 rid 被 30+ 个文案共用，
#: 全列出来单行就上千字符，把整份清单的预算吃掉——超出的部分折成「… 等 N 个」。
_LABEL_CAP = 8


def _quota_split(groups: list[list[str]], limit: int,
                 breadth_share: float = BREADTH_SHARE) -> list[str]:
    """一半预算按页轮转（广度），另一半按页顺序补（深度）。

    ⚠️ **不要改回纯轮转（`breadth_share=1.0`）**。2026-10-26 在真机图谱上扫过配比，
    两个指标一起看（活动覆盖数 / 16 条用例真机修正后的定位值有多少落在注入清单里）：

    | App | 0%（全深度） | 40–60% | 80% | 100%（纯轮转） |
    |---|---|---|---|---|
    | 资讯类 A | 1 act，召回 21/29 | **6 act，召回 21/29** | 6 act，19/29 | 6 act，**12/29** |
    | 社区类 B | 3 act，召回 6/8 | **9 act，召回 6/8** | 9 act，6/8 | 9 act，6/8 |

    ⇒ 40–60% 就把活动覆盖吃满、召回**一点不掉**；纯轮转把首页压到只剩 5 条，
    首页控件（频道栏/搜索框/列表项）被挤出清单，召回掉 43%。
    「覆盖更广」不等于「更好」——广度是手段，召回才是目的。
    """
    if limit <= 0:
        return []
    breadth = int(limit * breadth_share)
    out = _round_robin(groups, breadth) if breadth > 0 else []
    seen = set(out)
    if len(out) < limit:
        for group in groups:  # 深度：按页顺序补齐（第一页优先）
            for line in group:
                if line in seen:
                    continue
                out.append(line)
                seen.add(line)
                if len(out) >= limit:
                    return out
    return out


def _page_rid_lines(page: dict, text_limit: int) -> list[tuple[str, tuple]]:
    """一页的 res-id 行：**同一 rid 的多个文案合并成一行**（返回 (行, 去重键)）。

    ## 为什么必须合并（2026-10-28 起）

    旧实现按 `(rid, label)` 去重 ⇒ 一个共用 rid 会变成**每个文案一行**。真实清单里
    某资讯 App 首页的频道栏是这样注入的：

        - res-id: .../channelbar_text  文案: 7x24  页面: .activity.SplashActivity
        - res-id: .../channelbar_text  文案: 要闻  ...
        - res-id: .../channelbar_text  文案: 国际  ...        ← 同一 rid 出现 7 行

    读起来像"7 个恰好同 id 的控件"，模型据此认为 `text=国际` 与 `res-id=channelbar_text`
    是**两个等价选项**，于是写 `text=国际`——而 R21 立刻报"3 个同名控件"。
    **候选集没告诉它这是个共用容器**，这是注入形态的缺陷，不是模型偷懒。

    合并后：`- res-id: .../channelbar_text  文案: 7x24 | 要闻 | 关注 | ...（共用 rid，7 个文案）`
    ⇒ 模型能看出"这是一个控件组，要指定其中一个只能用文案"，或按顺序用 `index`。

    ## 顺序 = **首次出现顺序**，不按 bounds 排序

    实测该页频道栏有 **15 个** `channelbar_text` 控件，但只有 7 个不同文案——
    因为探索时频道栏**滚动过**，同一页里叠了两份快照（第二份里「关注」的 x 从 291 变成 0）。
    按 x 排序会把两份快照混着排（关注 0 / 7x24 0 / 要闻 153 …）⇒ 顺序全乱；
    而首次出现顺序恰好就是第一份快照的真实 tab 顺序。**别改成按 bounds 排。**

    ## 数量怎么写

    - 有多个文案：写**文案个数**，不写控件个数（15 是含重复快照的，写出来是误导）。
    - 无文案的多实例：写控件个数（没有文案可去重，这个数就是真的）。

    ## `index` 映射（2026-10-28 起，同一条规则的第二个半）

    `text=国际` 在这类页面上有 **3 个**同名控件（2 个频道 tab 的两份快照 + 1 个筛选弹窗
    的标签 `filter_dialog_tag_name`）⇒ R21 会报"改用 res-id"，但 `channelbar_text` 一个 rid
    管 9 个 tab、**写 res-id 等于点第一个** ⇒ 建议不可执行，模型只能退回 text，循环。

    解法：该页的 `channel_bar_item_container`（**可点**、无文案、15 个实例）与
    `channelbar_text` 的 15 个实例 **bounds 逐一完全相同** ⇒ 把 `index → 文案` 映射直接
    写出来，模型才可能写 `res-id=.../channel_bar_item_container, index=5`。

    ⚠️ 映射里的 `index` 是**该 rid 的实例序号**（0 基，含重复快照），不是"去重后第几个文案"。
    实测 15 个实例里 index 6 与 7 都是「娱乐」（同一 tab 的两种 bounds）——照实例序号列，
    别按去重文案列，否则模型算出来的 index 会整体偏移。
    """
    order: list[str] = []
    labels: dict[str, list[str]] = {}
    inst_labels: dict[str, list[str]] = {}
    count: dict[str, int] = {}
    bounds: dict[str, list[str]] = {}
    clickable: dict[str, int] = {}
    #: rid → **带 selected=true 的实例序号**（0 基，按实例顺序）。
    #: 用来在清单里标出"这个控件的 selected 是真机上可断言的"，以及"当前选中的是哪个实例"。
    selected_at: dict[str, list[int]] = {}
    for control in page.get("controls") or []:
        rid = str(control.get("res-id") or "").strip()
        # ⚠️ **退化 rid 不进 rid 行**（2026-10-28）：`0_resource_name_obfuscated` 被同页
        # 十几个**毫不相干**的控件共用（标题 / 点赞数 / 用户名 / tab …），合并成一行后
        # 既是 200+ 字符的噪声、又完全无法定位。实测 xhs 有 **48 行**这样的行，约占整份
        # 清单的 58% 字符；而且它把内容快照（`东胜野生眉安妮`、`点赞 15`）混进候选集，
        # 模型会误以为这些是可用定位值。⇒ 这些控件交给 `controls_context` 的**文案回落**分支。
        if not rid or is_degenerate_res_id(rid):
            continue
        label = (control.get("text") or control.get("desc") or "").strip()
        instance = count.get(rid, 0)          # 本控件的实例序号（先取后加）
        count[rid] = instance + 1
        bounds.setdefault(rid, []).append(str(control.get("bounds") or "").strip())
        inst_labels.setdefault(rid, []).append(label)
        if control.get("clickable"):
            clickable[rid] = clickable.get(rid, 0) + 1
        if control.get("selected"):
            selected_at.setdefault(rid, []).append(instance)
        if rid not in labels:
            order.append(rid)
            labels[rid] = []
        if label and label not in labels[rid]:
            labels[rid].append(label)
    # 有文案的 rid 里，谁能提供「实例序号 → 文案」：bounds 序列完全一致才行（同序同值）。
    # ⚠️ 值必须是 `inst_labels`（**含重复、按实例顺序**），不是去重后的 `labels`——
    # 用去重列表会让 index 整体偏移（15 个实例 vs 9 个文案）。
    by_seq: dict[tuple[str, ...], list[str]] = {}
    for rid in order:
        if len(labels[rid]) > 1 and all(bounds[rid]):
            by_seq.setdefault(tuple(bounds[rid]), inst_labels[rid])
    # 本页是否存在**可点**的无文案容器能提供 index 映射（决定"多文案行"该给什么建议）。
    has_clickable_map = any(
        count[rid] > 1 and not labels[rid] and clickable.get(rid, 0) >= count[rid] // 2
        and all(bounds[rid]) and tuple(bounds[rid]) in by_seq
        for rid in order)
    activity = str(page.get("activity") or "")[:40]

    def _sel_note(rid: str) -> str:
        """`★真机 selected` 标记（2026-10-28）。

        为什么要写进清单：切 tab 的正确断言是 `{"type":"selected", ...}`，但模型此前
        **无从判断哪个控件真的带 selected**——图谱全量 0 个 `selected=True`（控件字典原先
        只有 6 个字段）。猜错就是运行期 `get_state` 抛"节点无 selected 状态"⇒ 断言必红。
        ⇒ 只标**真机采到过**的，让模型能放心断；没标的就别断。

        多实例时还要给出**当前选中的是哪个实例**——那是"切换前"的基线，也是
        "到底切没切"的参照。
        """
        sel = selected_at.get(rid) or []
        if not sel:
            return ""
        if count[rid] <= 1:
            return "  ★真机 selected"
        return "  ★真机 selected: index=" + ",".join(str(i) for i in sel)

    out: list[tuple[str, tuple]] = []
    for rid in order:
        labs, n = labels[rid], count[rid]
        sel = _sel_note(rid)
        if len(labs) > 1:
            shown = labs[:_LABEL_CAP]
            joined = " | ".join(x[:text_limit] for x in shown)
            if len(labs) > _LABEL_CAP:
                joined += f" … 等 {len(labs)} 个"
            # ⚠️ **不要**再写"按此顺序用 index"：这里列的是**去重文案**（9 个），
            # 而 index 数的是**实例**（15 个，含重复快照）⇒ 两个顺序对不上，照它算必偏移。
            if has_clickable_map:
                note = (f"（共用 rid，{len(labs)} 个文案；text 会命中多个同名控件，"
                        "精确点某一个请用同页**可点容器**那行的 index）")
            else:
                note = f"（共用 rid，{len(labs)} 个文案；该 rid 无法单独指定，只能用 text=某文案）"
            line = f"- res-id: {rid}  文案: {joined}{note}{sel}  页面: {activity}"
        elif labs:
            # 单文案不写 index 提示：它不制造歧义，而"同 rid ×N"每行 ~20 字符，
            # 实测在某资讯 App 上给 120 行里的一大半都加上，字符数 +27% 却换不到任何覆盖增益。
            line = f"- res-id: {rid}  文案: {labs[0][:text_limit]}{sel}  页面: {activity}"
        else:
            note = f"（无文案 ×{n}）" if n > 1 else "（无文案）"
            line = f"- res-id: {rid}  {note}  页面: {activity}"
            if n > 1 and clickable.get(rid, 0) >= n // 2:
                # 只对**可点**的容器给映射：index 指向一个不可点的 View 对点击没用。
                seq_labels = by_seq.get(tuple(bounds[rid])) if all(bounds[rid]) else None
                if seq_labels:
                    shown = [f"{i}={t[:text_limit]}" for i, t in enumerate(seq_labels[:_LABEL_CAP])]
                    more = f" … 等 {n} 个" if n > _LABEL_CAP else ""
                    line = (f"- res-id: {rid}  {note} index→文案: {' '.join(shown)}{more}"
                            f"（index = 该 rid 的**实例序号**，0 基）{sel}"
                            f"  页面: {activity}")
            elif sel:
                line = f"- res-id: {rid}  {note}{sel}  页面: {activity}"
        # 去重键：多文案用整组（合并后是一行新东西）；单/无文案沿用旧的 (rid, 文案) 口径——
        # 把实例数 n 也放进键会让同一控件在不同页各留一行，实测字符数虚涨而活动覆盖不变。
        key = (rid, tuple(labs)) if len(labs) > 1 else (rid, labs[0] if labs else "")
        out.append((line, key))
    return out


def controls_context(pages: list[dict], max_controls: int = 120, text_limit: int = 30,
                     max_text_controls: int | None = None,
                     max_page_notes: int = 60) -> str:
    """索引 → 注入 prompt 的控件清单文本（去重、限量，防 prompt 膨胀）。

    两段式：res-id 行优先；**没有 res-id 的 clickable 控件回落到
    稳定文案行**（`- 文案: x  类型: clickable`）。真机数据显示 RN 容器页 res-id 覆盖率
    可低至 0%（实测某 RN 应用 2620 个控件只有 6 个 res-id）——只收 res-id 等于在 RN 应用上
    主动放弃防幻觉底座。DSL 的 text 定位策略链本就支持文案引用（gen.prompt 规则 7）。
    动态文案（"581 条"）不收：注入了也只是诱导模型生成脆弱定位。

    2026-10-13 起在最前面追加**页面用途段**（`page_purpose_lines`）——AI 写用例时
    先「看懂每个页面是干什么的」，减少定位错页面（同类页选错）的问题；仍以控件
    清单为主体，用途段只是导航头。

    ⚠️ **2026-10-26：配额改成「一半按页轮转、一半补深度」，不再让第一页吃光，也不是纯轮转。**
    原来是"按 pages 顺序往列表里塞，塞满 `max_controls` 就停"——第一个页面控件一多，
    后面的页面一条都进不去。真机实测（某资讯类 App 图谱 24 页 / 1499 控件）：**120 条 res-id
    配额被首页（600 控件）全部占满，模型只看到 24 页里的 1 页**；某电商 App 117 页只看到 8 页、
    内容社区类 App 55 页只看到 11 页。而模型被要求写"点进详情页 / 进搜索结果页 / 去我的页"——
    那些页面的控件它**一条都没见过**，只能凭先验编（一次同代重测的 49 次失败里
    41 次是"目标值根本不在候选集"，机制同源）。
    **但纯轮转是过冲**：它把首页压到只剩 5 条，首页控件被挤出清单，召回反掉 43%。
    标定后取 50/50（见 `_quota_split`）——**不增加 token 预算**，只改变"预算花在哪些页上"。
    """
    if max_text_controls is None:
        max_text_controls = max_controls
    seen_rid: set[tuple] = set()
    seen_label: set[str] = set()
    #: 逐页展开 → 全局去重（先到先得）→ 再由 `_quota_split` 一半轮转一半补深度。
    per_page_rid: list[list[str]] = []
    per_page_text: list[list[str]] = []
    for page in pages:
        activity = page.get("activity", "")
        page_texts = {str(c.get("text") or "").strip() for c in page.get("controls", [])}
        page_texts.discard("")
        rids: list[str] = []
        texts: list[str] = []
        for line, key in _page_rid_lines(page, text_limit):
            if key in seen_rid:
                continue
            seen_rid.add(key)
            rids.append(line)
        for control in page.get("controls", []):
            rid = control.get("res-id", "")
            label = (control.get("text") or control.get("desc") or "").strip()
            # 退化 rid 当**没有 rid**：这些控件（xhs 底部导航 `首页`/`我`/`发布` 就在里面）
            # 的 rid 是占位名、零区分度，但文案和 clickable 都是真的 ⇒ 必须能进文案段。
            if rid and is_degenerate_res_id(rid):
                rid = ""
            if rid or not (control.get("clickable") and label) or is_dynamic_text(label):
                continue
            # 部分动态（`消息，1条未读`）取稳定前缀，且**前缀要在本页真有 text**
            # ——`消息` 确实是同页 TextView 的 text，所以这条能收；`3人成团` 取不到 ⇒ 丢。
            if has_dynamic_segment(label):
                label = stable_prefix(label)
                if not label or label not in page_texts:
                    continue
            if label in seen_label:
                continue
            seen_label.add(label)
            texts.append(f"- 文案: {label[:text_limit]}  类型: clickable  页面: {activity[:40]}")
        if rids:
            per_page_rid.append(rids)
        if texts:
            per_page_text.append(texts)
    rid_lines = _quota_split(per_page_rid, max_controls)
    text_lines = _quota_split(per_page_text, max_text_controls)
    purpose = page_purpose_lines(pages, max_notes=max_page_notes)
    total_purpose = sum(1 for p in pages or [] if (p.get("summary") or "").strip())
    head = ""
    if purpose:
        more = total_purpose - len(purpose)
        tail = f"\n（另有 {more} 页描述未注入）" if more > 0 else ""
        head = ("【页面用途】\n" + "\n".join(purpose) + tail
                + "\n\n【控件清单】\n")
    lines = rid_lines + text_lines
    body = "\n".join(lines) if lines else "（索引为空：请先执行 index 命令构建）"
    return head + body


def controls_context_for(package: str, base_dir: Path | str | None = None) -> str:
    """读 `<base_dir>/<package>.json` → 控件清单；文件缺失/损坏返回 ""（调用方静默降级）。

    CLI 与 console 共用同一个入口。此前两处各写一份，结果 console 的 convert 漏了
    控件清单——等于默默放弃了防幻觉底座，且从 UI 上看不出任何异常。
    """
    base = Path(base_dir) if base_dir is not None else Path("artifacts/page_index")
    # A3（2026-10-12 评审）：package 来自用户输入，非法形状（绝对路径/分隔符）会
    # 重置或越出 base_dir。按"无图谱"静默降级——与文件缺失同语义，不惊扰调用方。
    from .pkgname import safe_package_name

    try:
        safe_package_name(package)
    except ValueError:
        return ""
    path = base / f"{package}.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    pages = data.get("pages")
    if isinstance(pages, dict):  # 防御：异常结构按 list 消费方要求转回
        pages = list(pages.values())
    if not isinstance(pages, list):
        return ""
    return controls_context(pages)


def controls_list_for(package: str, base_dir: Path | str | None = None,
                      max_controls: int | None = 300) -> list[dict]:
    """索引 → **结构化**控件清单（配置台编辑器的 target 下拉候选，发起人 2026-10-12 P0）。

    与 controls_context 同源同口径（同去重、同动态文案过滤、res-id 优先），只是输出
    结构化条目而非 prompt 文本。条目：{"res_id": 非空则为 res-id 候选否则为 text 候选,
    "label": 稳定文案, "page": activity, "clickable": bool}。
    文件缺失/损坏返回 []——调用方按「无清单」降级（前端允许手填并提示，不阻断）。

    ⚠️ **`max_controls=None` 表示不截断**（2026-10-26 加）。默认 300 是给**下拉候选**用的
    展示上限，但 `case_audit` 的 R18 拿它做**存在性判定**——用截断后的集合判"不在清单里"
    就是把没截到的页面全判成编造。实测某资讯类 App 图谱 1499 个控件，前 300 条**只覆盖 1 个
    activity**（首页），于是详情页的 `news_detail_title` / `action_bar_comment` /
    `title_bar_share_btn` 全被 R18 误报成"不在控件清单"——而这几个 rid 在真机上确实存在
    （同批用例 3/3 通过）。⇒ **展示要限量，判定不能限量。**
    """
    from .pkgname import safe_package_name

    try:
        safe_package_name(package)
    except ValueError:
        return []  # A3：形状非法按"无清单"降级，不给路径拼接原语
    base = Path(base_dir) if base_dir is not None else Path("artifacts/page_index")
    path = base / f"{package}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pages = data.get("pages")
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list):
        return []
    full = None if max_controls is None else max_controls
    items: list[dict] = []
    seen: set[tuple] = set()
    for page in pages:
        activity = str(page.get("activity", ""))
        # 该页所有 `text`：用来校验「部分动态文案的稳定前缀」在本页**真的有这个 text**
        # （`消息，1条未读` → `消息`，而 `消息` 确实是同页 TextView 的 text）。
        page_texts = {str(c.get("text") or "").strip() for c in page.get("controls", [])}
        page_texts.discard("")
        for control in page.get("controls", []):
            rid = str(control.get("res-id", "") or "")
            label = str(control.get("text") or control.get("desc") or "").strip()
            # ⚠️ **退化 rid ≠ 没有控件**（2026-10-28）：`0_resource_name_obfuscated` 只是
            # 名字零区分度（用它定位会命中第一个），但控件本身和它的文案是真的。
            # 原先这里 `continue` 把整条控件丢掉 ⇒ **文案也一起丢**。
            # 实测 xhs：924 个退化 rid 控件里有底部导航 `首页`/`我`/`发布`/`消息，1条未读`
            # （真机 dump 确认 `clickable=true`），而清单里一条都没有 ⇒ R18 把模型写**对**的
            # `text=我` 报成"不在控件清单"，生成侧也拿不到这些入口。
            # ⇒ 退化 rid 当**没有 rid** 处理，让它落到下面的文案分支。
            if rid and is_degenerate_res_id(rid):
                rid = ""
            if rid:
                if ("rid", rid, label) in seen:
                    continue
                seen.add(("rid", rid, label))
                items.append({"res_id": rid, "label": label[:30],
                              "page": activity[:60], "clickable": bool(control.get("clickable")),
                              "selected": bool(control.get("selected"))})
            elif control.get("clickable") and label and not is_dynamic_text(label):
                if has_dynamic_segment(label):
                    label = stable_prefix(label)
                    if not label or label not in page_texts:
                        continue      # 前缀在本页没有对应 text ⇒ 不猜（宁缺毋滥）
                if ("txt", label) in seen:
                    continue
                seen.add(("txt", label))
                items.append({"res_id": "", "label": label[:30],
                              "page": activity[:60], "clickable": True,
                              "selected": bool(control.get("selected"))})
            if full is not None and len(items) >= full:
                break
        if full is not None and len(items) >= full:
            break
    return items


def home_labels_for(package: str, base_dir: Path | str | None = None,
                    max_depth: int = 1) -> tuple[set[str], set[str]]:
    """首页层级（depth ≤ max_depth）页面的 (res-id 集, 可点稳定文案集)。

    供「首步可达性」启发式（case_audit L5）使用：冷启动后第一个交互步骤的
    目标应能在首页层级找到，否则疑似缺少前置导航。与 controls_list_for 同源
    同口径（退化 res-id 过滤、动态文案过滤）。文件缺失/损坏返回空集——调用方
    按「无索引」跳过该校验，不瞎猜。
    """
    from .pkgname import safe_package_name

    try:
        safe_package_name(package)
    except ValueError:
        return set(), set()  # A3：形状非法按"无索引"降级
    base = Path(base_dir) if base_dir is not None else Path("artifacts/page_index")
    path = base / f"{package}.json"
    if not path.exists():
        return set(), set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set()
    pages = data.get("pages")
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list):
        return set(), set()
    rids: set[str] = set()
    texts: set[str] = set()
    for page in pages:
        depth = page.get("depth")
        depth = 0 if not isinstance(depth, int) else depth
        if depth > max_depth:
            continue
        for control in page.get("controls", []):
            rid = str(control.get("res-id", "") or "")
            if rid and not is_degenerate_res_id(rid):
                rids.add(rid)
            label = str(control.get("text") or control.get("desc") or "").strip()
            if label and control.get("clickable") and not is_dynamic_text(label):
                texts.add(label)
    return rids, texts


#: 页面用途（summary）在页面结构上下文里的截断上限。describe 要求 summary ≤40 字，
#: 这里留同样的余量：`max_pages=12` 时最多多出 ~500 字符，相对控件清单仍是很小的一块。
GRAPH_SUMMARY_CHARS = 40


def _one_line(text, limit: int = 0) -> str:
    """把可能多行/带多余空白的字段压成单行（超长截断加省略号）。

    为什么必须做：这份上下文是**单行拼接**（`" → ".join(...)`），而 `summary` 是模型
    生成的自由文本——真跑里出现过换行与连续空格。不归一就会把一条记录劈成两行，
    读起来像两个页面（且长度不可控）。
    """
    if not isinstance(text, str):
        return ""
    out = re.sub(r"\s+", " ", text).strip()
    if limit and len(out) > limit:
        out = out[:limit - 1] + "…"
    return out


def graph_context(pages: list[dict], max_pages: int = 12, with_summary: bool = True) -> str:
    """图谱页面 → 注入 prompt 的页面结构上下文（严格限量防 prompt 膨胀）。

    **取哪 12 页比取多少页更重要**（实测的三个缺陷，T54 修正）：
    - **按 identities 去重**：page_key 不稳会让同屏记成多条（真实 App 实测 131 页 ≈ 86 个功能），
      不去重时 12 个槽位里 3 个浪费在同一页；
    - **有 parent_action 的页优先**：没有它就没有「点[X] →」导航边，上下文退化成平铺标签
      （实测 79% 的页没有 parent_action ⇒ 默认排序下输出里一条导航边都没有）；
    - **depth 缺失不静默丢弃**：标 `L?`（曾用 `isinstance(depth, int)` 把 5 页静默滤掉）。

    **每页带上它的用途（`summary`）**（2026-10-16）：此前只注入 `title`，等于只告诉 AI
    「这页叫什么」，不说「这页是干什么的」——而 `summary` 才是 `describe` 花额度换来的
    业务理解（例："搜索商家与商品，可看历史搜索与热搜榜"）。评测线的零成本核查正是
    卡在这里：注入内容太弱时，A/B 无论正负都不可解释。缺 `summary`（未跑 describe）时
    输出与旧版**逐字一致**，不改动未描述图谱的行为。

    `with_summary=False` ⇒ **只给页名**（= 2026-10-16 之前的行为）。这个参数只为
    **三臂消融**服务：不把"页名"这一层单独跑一臂，就无法证明效果来自 `summary`
    而不是来自页名（评测总结过的结论缺口）。
    纯函数：不读盘、不看环境变量，便于 CLI / console 共用与单测。
    """
    def dedup_key(p: dict):
        ids = p.get("identities") or []
        if ids:
            return ("ids", frozenset(ids))
        if p.get("title"):
            return ("title", p["title"])
        return ("key", p.get("page_key", ""))

    seen: set = set()
    unique: list[dict] = []
    for p in pages:
        if not isinstance(p, dict):
            continue
        k = dedup_key(p)
        if k in seen:
            continue
        seen.add(k)
        unique.append(p)

    # 有 parent_action 的页优先（导航边是这份上下文最值钱的部分），同组内浅层在前
    unique.sort(key=lambda p: (0 if p.get("parent_action") else 1,
                               p["depth"] if isinstance(p.get("depth"), int) else 99))
    parts: list[str] = []
    for page in unique[:max_pages]:
        structure = page.get("structure") or {}
        label = (page.get("title") or ""
                 or (structure.get("key_elements") or [""])[0]
                 or page.get("page_key", "")[:8])
        depth = page.get("depth")
        depth_txt = f"L{depth}" if isinstance(depth, int) else "L?"
        suffix = f"{label}({page.get('node_count', 0)} 控件)"
        summary = _one_line(page.get("summary"), GRAPH_SUMMARY_CHARS) if with_summary else ""
        if summary:
            suffix += f"：{summary}"
        parts.append(f"点[{page['parent_action']}] → {depth_txt} {suffix}"
                     if page.get("parent_action") else f"{depth_txt} {suffix}")
    return "【页面结构】" + " → ".join(parts)


def graph_context_for(package: str, base_dir: Path | str | None = None,
                      max_pages: int = 12) -> str:
    """从 `<base_dir>/<package>.json` 读图谱并产出页面结构上下文。

    默认**关闭**，`GEN_GRAPH_CONTEXT=1` 开启：这是会改变 AI 生成输出的增强，
    应先做真机 A/B 对照再决定是否默认打开（诚实边界）。

    `GEN_GRAPH_SUMMARY=0` 可**只注入页名、不注入页面用途**（默认 1）。
    这个开关存在的唯一目的是做**三臂消融**：`off`（无注入）/ `title`（只有页名，
    即 2026-10-16 之前的行为）/ `full`（页名+用途）。没有它就无法把效果归因到
    `summary` 本身——页名在改动前就已经注入了（评测记录在案）。
    """
    if os.environ.get("GEN_GRAPH_CONTEXT", "0").strip().lower() not in ("1", "true", "yes"):
        return ""
    with_summary = os.environ.get("GEN_GRAPH_SUMMARY", "1").strip().lower() in ("1", "true", "yes")
    base = Path(base_dir) if base_dir is not None else Path("artifacts/page_index")
    # A3 同口径（见 controls_context_for）：非法包名按"无图谱"静默降级。
    from .pkgname import safe_package_name

    try:
        safe_package_name(package)
    except ValueError:
        return ""
    index = load_index(base / f"{package}.json")
    if not index:
        return ""
    return graph_context(index.get("pages", []), max_pages=max_pages,
                         with_summary=with_summary)


_PERMISSION_PKGS = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.packageinstaller",
)


def _dismiss_permission_dialog(drv) -> bool:
    """清掉系统权限弹窗（不授权，效果等同「禁止」）。

    弹窗残留是重建失败的元凶之一（2026-10-12 实测：津云录音权限弹窗挂了半小时
    ——back 对华为弹窗无效，App 被拉起后一直在等授权结果，前台永远被
    permissioncontroller 占着 ⇒ _ensure_foreground 超时 ⇒ 0 控件报错）。
    force-stop permissioncontroller = 取消授权请求，挂起的 App 自动回前台。
    **不点「允许」**：爬取/探索不需要权限功能，授权反而可能引发录音/相机等连锁页面。
    """
    try:
        if (drv.foreground_app() or {}).get("package") not in _PERMISSION_PKGS:
            return False
    except Exception:
        return False
    try:
        drv.shell("am force-stop com.android.permissioncontroller")
        _pause(0.5)
        return True
    except Exception:
        pass
    try:
        drv.press("back")
        return True
    except Exception:
        return False


def _ensure_foreground(drv, package: str, timeout_s: float = 12.0) -> bool:
    """把被测 App 拉到前台（crawl 前置，2026-10-12 用户报「重建页面索引执行失败」）。

    crawl 只按包名过滤**前台**控件树——App 不在前台时 dump 到的全是桌面控件，
    in_package 过滤后 0 控件 ⇒ 响亮报错。产品口径（2026-10-10 起）：重建索引
    「执行期间会占用设备并打开被测 App」，拉起是引擎职责而非用户手动前置。

    用 monkey 拉起（无需知道 launcher activity，通用性最好）；轮询 foreground_app
    等就绪；期间清掉残留/新弹的系统权限弹窗。已在前台走快速路径（零 sleep）。
    超时/失败返回 False 不阻断——照旧爬取，让上游的空图谱响亮报错兜底。
    """
    try:
        if (drv.foreground_app() or {}).get("package") == package:
            return True
    except Exception:
        pass
    _dismiss_permission_dialog(drv)  # 残留弹窗会挡住拉起：App 在弹窗下等授权结果
    try:
        drv.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
    except Exception:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _pause(0.8)
        try:
            if (drv.foreground_app() or {}).get("package") == package:
                return True
        except Exception:
            pass
        _dismiss_permission_dialog(drv)  # 拉起过程中 App 自己弹的权限请求也一并清掉
    return False


def crawl(drv, package: str, max_steps: int = 30, max_pages: int = 15,
          on_progress=None, shots_dir: Path | None = None) -> dict:
    """轻量爬取：逐页记录控件摘要，点击未访问的可点击元素向外探索（带敏感黑名单）。

    停止条件：步数/页数预算用尽，或连续 back 仍无新页面。
    `shots_dir`：页面截图落盘目录；约定相对 page_index 目录的
    `screenshots/<package>/<page_key>.png`（与 explore 增量回填、web_api/review
    的 screenshot_url 拼接、引擎侧 review 缩略图三处口径一致）。None=不截图（CLI 兼容）。
    """
    _ensure_foreground(drv, package)  # 被测 App 不在前台 ⇒ 全是桌面控件，0 控件必现
    visited: set[str] = set()
    tried: set[tuple[str, str]] = set()  # (page_key, bounds) 已点击过
    tried_by_key: dict[str, set[str]] = {}  # page_key → bounds 集合（落盘进页面记录，供 explore 增量认页复用）
    pages: list[dict] = []
    backs = 0

    def _clickable_candidates(xml: str) -> list[dict]:
        out = []
        for node in parse_nodes(xml):
            if not node.clickable or not node.in_package(package):
                continue
            label = node.label
            if any(word in label for word in _SENSITIVE):
                continue  # 防误点黑名单
            if node.cls == "android.widget.EditText":
                continue  # 不点输入框（避免污染后续页面）
            # **广告位不点**（2026-10-23）：点信息流广告卡片会把设备跳到应用市场
            # （`explore` 一轮实测离场 18 次）。判据只看 rid 且刻意窄，见 `safety.is_ad_slot_rid`。
            if is_ad_slot_rid(node.rid):
                continue
            out.append({"bounds": node.bounds, "label": label, "rid": node.rid})
        return out

    for _ in range(max_steps):
        xml = drv.dump()
        app = {}
        try:
            current = drv.foreground_app()
            app = {"package": current.get("package"), "activity": current.get("activity")}
        except Exception:
            pass
        # **广告落地页不登记**（2026-10-23，与 explore 同一判据）：`mat index` 也会点到
        # 信息流广告，前台仍是被测 App 自己（`WebAdvertActivityV2`）⇒ 离场检测抓不到。
        # 实测 tx-news 的图谱 `sources: ["index","explore"]`，53 页里 15 条广告页——
        # 两个生产入口都得拦，否则"只修 explore"等于没修（同"闸门只有一处实现"的纪律）。
        if is_ad_landing({"activity": app.get("activity") or ""}):
            try:
                drv.press("back")
            except Exception:
                break
            backs += 1
            if backs >= 3:
                break          # 回不去（广告页常自带返回按钮拦 back）⇒ 结束，别空转
            _pause(1.2)
            continue

        key = page_key(xml, package)
        digest = digest_page(xml, package)
        # 0 控件页不入图谱（2026-10-12）：crawl 末段 back 会退到桌面/锁屏等非被测界面，
        # 此前照样记页 ⇒ 复核页混进空页条目（实测华为桌面 .unihome.UniHomeLauncher 0 控件页）。
        # 空图谱报错口径不变：汇总 node_count == 0 仍响亮失败。
        if key not in visited and digest.get("node_count", 0) > 0:
            visited.add(key)
            # 页面截图（2026-10-12 用户报复核页截图列全「无」）：crawl 此前从未拍图，
            # 复核页 UI 有截图列却永远空。紧贴 dump 同帧拍——此刻前台就是这一页
            # （下一步就可能点走）。失败不阻断爬取（同 explore 截图防崩纪律），
            # 该页 screenshot=None 复核页照旧显示「无」。
            shot_rel = None
            if shots_dir is not None:
                try:
                    shots_dir.mkdir(parents=True, exist_ok=True)
                    shot_file = shots_dir / f"{key}.png"
                    drv.screenshot(str(shot_file))
                    if shot_file.exists() and shot_file.stat().st_size > 0:
                        shot_rel = f"screenshots/{package}/{key}.png"
                except Exception:
                    shot_rel = None
            pages.append({
                "page_key": key,
                "activity": app.get("activity", ""),
                "tried": sorted(tried_by_key.get(key, set())),
                "screenshot": shot_rel,
                **digest,
            })
            backs = 0
            if on_progress:
                on_progress(len(pages), max_pages)
            if len(pages) >= max_pages:
                break

        candidates = [a for a in _clickable_candidates(xml)
                      if (key, tried_entry(a)) not in tried]
        if not candidates:
            if backs >= 3:
                break  # 连续 back 无新页，结束
            try:
                drv.press("back")
            except Exception:
                break
            backs += 1
            _pause(1.2)
            continue

        target = candidates[0]
        tried.add((key, tried_entry(target)))
        tried_by_key.setdefault(key, set()).add(tried_entry(target))
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", target["bounds"])
        if match:
            x1, y1, x2, y2 = (int(g) for g in match.groups())
            drv.click((x1 + x2) // 2, (y1 + y2) // 2)
        _pause(1.5)

    # tried 回写：页面在建页时刻没有点击记录（点击发生在记录之后），
    # 离开循环后把每页实际点过的 bounds 补进页面记录——explore 增量认页的数据源
    for pg in pages:
        pg["tried"] = sorted(tried_by_key.get(pg["page_key"], set()))
    return {
        "package": package,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "page_count": len(pages),
        "pages": pages,
    }


# -- 页面图谱（P0：schema v2 + 并集合并） -----------------------------------------

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _earliest(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return min(present) if present else None


def _latest(*values: str | None) -> str | None:
    present = [v for v in values if v]
    return max(present) if present else None


def _dedup_entries(entries: list) -> list[dict]:
    """alt_entries 去重（按 父页 + 到达动作 + 动作资源 id）。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        pair = (entry.get("parent_key"), entry.get("parent_action"),
                entry.get("parent_rid") or "")
        if pair in seen:
            continue
        seen.add(pair)
        item = {"parent_key": pair[0], "parent_action": pair[1]}
        if pair[2]:
            item["parent_rid"] = pair[2]
        out.append(item)
    return out


def _norm_health(health: dict | None) -> dict:
    out: dict = {}
    for key, value in (health or {}).items():
        if not isinstance(value, dict):
            continue
        out[key] = {"ok": int(value.get("ok", 0)), "fail": int(value.get("fail", 0)),
                    "last_ok_at": value.get("last_ok_at"), "last_fail_at": value.get("last_fail_at")}
    return out


def _merge_health(old: dict | None, new: dict | None) -> dict:
    out = _norm_health(old)
    for key, value in _norm_health(new).items():
        cur = out.setdefault(key, {"ok": 0, "fail": 0, "last_ok_at": None, "last_fail_at": None})
        cur["ok"] += value["ok"]
        cur["fail"] += value["fail"]
        cur["last_ok_at"] = _latest(cur["last_ok_at"], value["last_ok_at"])
        cur["last_fail_at"] = _latest(cur["last_fail_at"], value["last_fail_at"])
    return out


def _normalize_page(page: dict, now: str) -> dict:
    """补齐图谱字段并规整类型（旧平表页记录也能安全并入）。"""
    normalized = dict(page)
    normalized.setdefault("page_key", "")
    normalized.setdefault("activity", "")
    normalized.setdefault("depth", None)
    normalized.setdefault("parent_key", None)
    normalized.setdefault("parent_action", None)
    normalized.setdefault("parent_rid", None)
    normalized.setdefault("controls", [])
    normalized.setdefault("node_count", len(normalized.get("controls") or []))
    normalized.setdefault("screenshot", None)
    normalized.setdefault("app_version", "")
    normalized.setdefault("title", "")
    normalized.setdefault("title_source", "")     # "" | "heuristic" | "manual" | "ai"
    normalized.setdefault("summary", "")
    normalized.setdefault("summary_source", "")   # 同上；标注来源便于人工核对
    normalized["first_seen"] = normalized.get("first_seen") or now
    normalized["last_seen"] = normalized.get("last_seen") or now
    normalized["hits"] = int(normalized.get("hits", 1) or 0)
    normalized["tried"] = sorted({str(b) for b in (normalized.get("tried") or [])})
    # 页面信号集合（explore 的 `_identities` 口径）。存进图谱是为了让**下一次探索**能按
    # 「信号包含度」认页，而不是按 `page_key` 精确相等——后者跨轮不稳（实测同页稳定性仅 12%），
    # 导致 `tried` 永远匹配不上、增量探索静默失效。
    normalized["identities"] = sorted({str(s) for s in (normalized.get("identities") or [])})
    normalized["alt_entries"] = _dedup_entries(normalized.get("alt_entries") or [])
    normalized["step_health"] = _norm_health(normalized.get("step_health") or {})
    # 结构描述零 AI 成本，缺则就地补（index 爬取与 explore 两条路都自动获得）
    structure = normalized.get("structure")
    if not isinstance(structure, dict) or not (structure.get("key_elements")
                                              or structure.get("layout_regions")):
        normalized["structure"] = describe_structure(normalized.get("controls") or [])
    # identities 同理就地补：mat index 建的图谱若没有它，包含度认页在"首次使用主路径"上失效
    if not normalized.get("identities"):
        normalized["identities"] = sorted(
            identities_from_controls(normalized.get("controls") or []))
    normalized.pop("step", None)  # 本次探索的步序是运行期信息，不属跨会话图谱
    return normalized


def _merge_page(old: dict, new: dict) -> dict:
    """同 page_key 并集：控件取最新、深度取最浅、tried/alt_entries 取并集、计数累加。"""
    merged = dict(old)
    merged["controls"] = new.get("controls") or old.get("controls", [])
    merged["node_count"] = new.get("node_count", old.get("node_count", 0))
    for field in ("activity", "screenshot", "app_version"):
        if new.get(field):
            merged[field] = new[field]

    # depth 取更浅；父链随之更新为「更浅到达」的那条。
    # 但更浅到达若**不可重放**（parent_action 为空，back/直达场景），不替换——
    # 否则一次 back 到达就会把可点击的父链抹成断链（实测 tx-news 深层页 75/99
    # 因此缺 parent_action、arrival 无法重放）。depth 与父链一体，不换链就不换深度。
    old_depth, new_depth = old.get("depth"), new.get("depth")
    if new_depth is not None and (old_depth is None or new_depth < old_depth):
        new_ok = hop_replayable(new.get("parent_action"), new.get("parent_rid"))
        old_ok = hop_replayable(old.get("parent_action"), old.get("parent_rid"))
        if new_ok or not old_ok or old_depth is None:
            merged["depth"] = new_depth
            merged["parent_key"] = new.get("parent_key")
            merged["parent_action"] = new.get("parent_action")
            merged["parent_rid"] = new.get("parent_rid")
    elif (new_depth is not None and new_depth == old_depth
          and hop_replayable(new.get("parent_action"), new.get("parent_rid"))
          and not hop_replayable(old.get("parent_action"), old.get("parent_rid"))):
        # 同深度升级：旧链的到达动作是 iconfont 字符/空且无 rid（重放必死，第 4 层
        # 实测 0/25），新观测带可重放通道 ⇒ 换链保深度。iconfont 字符看着「有文案」，
        # 旧的「有 parent_action 就不换」判据会让死链永久占坑——重探永远修不了它。
        merged["depth"] = new_depth
        merged["parent_key"] = new.get("parent_key")
        merged["parent_action"] = new.get("parent_action")
        merged["parent_rid"] = new.get("parent_rid")
        merged.pop("alias_of", None)   # 链已换，旧借链留档随链作废（下次 repair 重算）
        merged.pop("alias_sim", None)
    elif old_depth is not None:
        merged["depth"] = old_depth
    else:
        merged["depth"] = new_depth

    merged["first_seen"] = _earliest(old.get("first_seen"), new.get("first_seen"))
    merged["last_seen"] = _latest(old.get("last_seen"), new.get("last_seen"))
    merged["hits"] = int(old.get("hits", 0)) + int(new.get("hits", 0))
    # `tried` 并集，但**丢掉老格式条目**（只有坐标、没有 label/rid）：它们不可验证，
    # 留着只会在每轮被丢弃一次（噪声）——本轮既然重新观测过这页，就让新观测说了算。
    merged["tried"] = sorted({e for e in (set(old.get("tried") or []) | set(new.get("tried") or []))
                              if not is_legacy_tried(e)})
    # 滚动探测"到底"是事实（2026-10-15）：一旦成立跨轮保持——旧页 True、新页 False 不回退
    merged["scroll_done"] = bool(old.get("scroll_done")) or bool(new.get("scroll_done"))
    # findings 累加（T19）：崩溃/白屏/离场计数是"高风险页"的依据，跨轮只增不减
    old_f, new_f = old.get("findings"), new.get("findings")
    if isinstance(old_f, dict) or isinstance(new_f, dict):
        fmerged = dict(old_f) if isinstance(old_f, dict) else {}
        for ftype, count in (new_f or {}).items():
            fmerged[ftype] = int(fmerged.get(ftype, 0)) + int(count)
        merged["findings"] = fmerged
    # `identities` 取**最新**而不是并集：并集会让信号集合越滚越大，而包含度的分母是
    # max(|A|,|B|)——集合膨胀会**反向压低**包含度，把"同一页"越认越不像。
    merged["identities"] = new.get("identities") or old.get("identities", [])
    merged["alt_entries"] = _dedup_entries(
        list(old.get("alt_entries") or []) + list(new.get("alt_entries") or []))
    merged["step_health"] = _merge_health(old.get("step_health"), new.get("step_health"))

    # title / summary 是「人工/AI 补全」的字段：空值绝不覆盖已有内容，
    # 且人工标题优先于 AI 标题（"manual" 不被 "ai" 顶掉）。
    if new.get("title") and (not old.get("title") or old.get("title_source") != "manual"):
        merged["title"] = new["title"]
        merged["title_source"] = new.get("title_source") or "ai"
    if new.get("summary") and (not old.get("summary") or old.get("summary_source") != "manual"):
        merged["summary"] = new["summary"]
        merged["summary_source"] = new.get("summary_source") or "ai"

    new_structure = new.get("structure") or {}
    if new_structure.get("key_elements") or new_structure.get("layout_regions"):
        merged["structure"] = new_structure
    return merged


def _normalize_stored(pages: list, fallback_ts: str) -> list:
    """规整**已落盘**的页面（旧 v1 平表 / 早期 v2 记录）。

    没有这一步，旧图谱里的页面就**永远没有 `structure`**——`mat describe` 只能拿到空的
    `key_elements`，起出来的名字全靠编（真实数据踩到：凌晨 `mat index` 建的 v1 索引 6 页
    全部缺 structure）。这里用页面自带的 `controls` 就地零 AI 补算。
    """
    return [_normalize_page(p, fallback_ts) if isinstance(p, dict) else p for p in pages]


def new_index(package: str) -> dict:
    """空图谱（schema v2）。"""
    now = _now()
    return {"schema": SCHEMA_VERSION, "package": package, "generated_at": now,
            "updated_at": now, "sources": [], "pages": [], "edges": []}


def load_index(path) -> dict | None:
    """读图谱；v1 平表自动升级为 v2。文件缺失/损坏返回 None（调用方静默降级）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pages = data.get("pages")
    if isinstance(pages, dict):  # 防御：万一被写成 dict，按 list 消费方要求转回 list
        pages = list(pages.values())
    if not isinstance(pages, list):
        pages = []
    if data.get("schema") == SCHEMA_VERSION:
        data["pages"] = _normalize_stored(pages, data.get("generated_at") or _now())
        data.setdefault("sources", ["index"])
        data.setdefault("edges", [])
        return data
    generated = data.get("generated_at") or _now()
    return {"schema": SCHEMA_VERSION, "package": data.get("package", ""),
            "generated_at": generated,
            "updated_at": data.get("updated_at") or generated,
            "sources": ["index"],
            "pages": _normalize_stored(pages, generated), "edges": []}


def save_index(path, index: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def keyed_pages(index: dict) -> dict[str, dict]:
    """`page_key → 页`，**并把 `aliases` 的旧 key 也指向代表页**。

    为什么必须解析 alias（2026-10-29）：`mat fragments --apply` 会**删掉**被并页，
    只在 `index["aliases"]` 里留档。运行期拿到的是**活页面的 page_key**——它可能正好是
    被删掉的那个碎片（合并前记进图谱的那一份）。此时 `pages_by_key.get(key)` 是 None，
    `pages_equivalent(None, target)` 恒 False ⇒ 置备/走链会认为"没到目标页"，
    于是**多走一跳或直接判失败**，而现象看起来像"定位值写错"。

    合并前这个窗口是存在的（命中已记录的碎片就能认出），合并后不解析 alias 就等于
    **把那条路封死**。所以凡是"按 key 反查页"的地方都该用本函数。
    """
    out: dict[str, dict] = {}
    for page in (index or {}).get("pages", []) or []:
        if isinstance(page, dict) and page.get("page_key"):
            out[page["page_key"]] = page
    for old, info in ((index or {}).get("aliases") or {}).items():
        rep = out.get((info or {}).get("of") or "")
        if rep is not None and old not in out:
            out[old] = rep
    return out


def merge_pages(index: dict, incoming: list[dict], source: str = "index") -> dict:
    """按 page_key 并集合并页面。

    - 已存在的页：`_merge_page`（控件取最新、depth 取最浅、tried 并集、hits 累加）
    - 新页：规整后追加
    - `pages` **保持 list**（顺序 = 首次出现顺序），不改成 dict
    """
    if not isinstance(index, dict):
        raise TypeError("index 必须是 dict")
    index.setdefault("schema", SCHEMA_VERSION)
    index.setdefault("package", "")
    pages = index.get("pages")
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list):
        pages = []
    now = _now()

    by_key: dict[str, dict] = {}
    order: list[str] = []
    fallback_ts = index.get("generated_at") or now
    for page in _normalize_stored(pages, fallback_ts):  # 保证在内存里的图谱始终已规整
        if not isinstance(page, dict):
            continue
        key = page.get("page_key")
        if not key:
            continue
        by_key[key] = page
        order.append(key)

    for raw in incoming or []:
        if not isinstance(raw, dict):
            continue
        page = _normalize_page(raw, now)
        key = page.get("page_key")
        if not key:
            continue
        if key in by_key:
            by_key[key] = _merge_page(by_key[key], page)
        else:
            by_key[key] = page
            order.append(key)

    index["pages"] = [by_key[k] for k in order if k in by_key]
    sources = index.setdefault("sources", [])
    if source not in sources:
        sources.append(source)
    index["schema"] = SCHEMA_VERSION
    index.setdefault("generated_at", now)
    index["updated_at"] = now
    return index


def merge_edges(index: dict, edges: list[dict]) -> dict:
    """并集合并导航边（`from → to` + 到达动作，计数累加）——完整跳转图。

    与页面的 `parent_key`（最小深度 BFS 树 = 最短复现）**互为补充**：
    前者答"最短怎么到"，这里答"总共观察到哪些跳转"（覆盖率、多入口、备选路径）。
    """
    if not isinstance(index, dict):
        raise TypeError("index 必须是 dict")
    existing = index.get("edges")
    if not isinstance(existing, list):
        existing = []

    by_key: dict[tuple, dict] = {}
    order: list[tuple] = []
    for edge in list(existing) + list(edges or []):
        if not isinstance(edge, dict):
            continue
        source_key, target_key = edge.get("from"), edge.get("to")
        if not source_key or not target_key:
            continue
        action = edge.get("action") or ""
        key = (source_key, target_key, action)
        if key in by_key:
            by_key[key]["count"] = int(by_key[key].get("count", 1)) + int(edge.get("count", 1) or 1)
        else:
            by_key[key] = {"from": source_key, "to": target_key, "action": action,
                           "count": int(edge.get("count", 1) or 1)}
            order.append(key)

    index["edges"] = [by_key[k] for k in order]
    index["updated_at"] = _now()
    return index


def update_page(index: dict, page_key: str, **fields) -> bool:
    """就地更新单个页面的指定字段。

    与 `merge_pages` 的区别：**不累加 `hits`、不补 `structure`**——用于「补描述」这类
    非观测性写入（`mat describe` / 人工补标题），避免把一次标注算成一次页面访问。
    """
    for page in index.get("pages", []):
        if page.get("page_key") == page_key:
            page.update(fields)
            index["updated_at"] = _now()
            return True
    return False


def load_or_crawl(drv, package: str, index_path: Path, refresh: bool = False,
                  max_steps: int = 30, max_pages: int = 15) -> tuple[dict, bool]:
    """读取既有图谱（v1 自动升级）；refresh 或不存在时真机爬取并 merge 落盘。

    返回 (图谱, 是否新建)。爬取结果以 source="index" 并入，保留 explore 已沉淀的层级字段。
    """
    index_path = Path(index_path)
    if not refresh:
        existing = load_index(index_path)
        if existing is not None:
            return existing, False
    index = new_index(package)
    # 截图目录与图谱 JSON 同源（<page_index>/screenshots/<package>/）——
    # explore 增量回填（explore.py graph_shots）与 web_api/review 的 screenshot_url
    # 都按这个约定拼路径，crawl 直接产出同口径落盘
    crawled = crawl(drv, package, max_steps=max_steps, max_pages=max_pages,
                    shots_dir=index_path.parent / "screenshots" / package)
    merge_pages(index, crawled.get("pages", []), source="index")
    save_index(index_path, index)
    return index, True
