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
    re.compile(r"^(?:刚刚|昨天|前天)$"),                                      # 相对时间
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
def is_degenerate_res_id(res_id: str) -> bool:
    """资源名是不是「占位/无区分度」的那种（只按**名字**判，不看包名）。"""
    name = (res_id or "").rsplit("/", 1)[-1].strip()
    return bool(name) and name[0].isdigit()


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
    return frozenset(signals)


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

def rid_hints(index_path: Path, res_id: str, limit: int = 4) -> list[str]:
    """从页面图谱反查某个 res-id 曾经对应的**可见文案**（供「改 id 后零成本自愈」使用）。

    为什么需要这一条：`res-id` 改名是真实 App 改版**最常见**的形式，而这类失效
    **没有文案线索**——通用本地模糊自愈从定位器推导出的 token 是 id 片段（`login_btn`），
    匹配不到界面文案（"登录"）。图谱里恰好有历史映射（`res-id → text/desc`），
    正好补上这条线索，而且是零成本的。

    返回顺序：**可点击控件的文案优先**（同一 id 若在多页出现，先给最可能是操作目标的那条）。
    """
    try:
        data = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    clickable: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for page in data.get("pages") or []:
        for control in page.get("controls") or []:
            if control.get("res-id") != res_id:
                continue
            label = (control.get("text") or control.get("desc") or "").strip()
            # 去重必须**跨桶**做：同一文案可能同时出现在可点击与不可点击的节点上，
            # 按桶去重会让它在 clickable + other 拼接后重复出现。
            if not label or label in seen or is_dynamic_text(label):
                continue
            seen.add(label)
            (clickable if control.get("clickable") else other).append(label)
    return (clickable + other)[:limit]


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
    """
    if max_text_controls is None:
        max_text_controls = max_controls
    rid_lines: list[str] = []
    text_lines: list[str] = []
    seen_rid: set[tuple] = set()
    seen_label: set[str] = set()
    for page in pages:
        activity = page.get("activity", "")
        for control in page.get("controls", []):
            rid = control.get("res-id", "")
            label = (control.get("text") or control.get("desc") or "").strip()
            if rid:
                if (rid, label) in seen_rid:
                    continue
                seen_rid.add((rid, label))
                rid_lines.append(f"- res-id: {rid}  文案: {label[:text_limit]}  页面: {activity[:40]}")
                if len(rid_lines) >= max_controls:
                    break
            elif (control.get("clickable") and label
                  and not is_dynamic_text(label) and label not in seen_label):
                seen_label.add(label)
                text_lines.append(f"- 文案: {label[:text_limit]}  类型: clickable  页面: {activity[:40]}")
                if len(text_lines) >= max_text_controls:
                    break
        if len(rid_lines) >= max_controls and len(text_lines) >= max_text_controls:
            break
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
                      max_controls: int = 300) -> list[dict]:
    """索引 → **结构化**控件清单（配置台编辑器的 target 下拉候选，发起人 2026-10-12 P0）。

    与 controls_context 同源同口径（同去重、同动态文案过滤、res-id 优先），只是输出
    结构化条目而非 prompt 文本。条目：{"res_id": 非空则为 res-id 候选否则为 text 候选,
    "label": 稳定文案, "page": activity, "clickable": bool}。
    文件缺失/损坏返回 []——调用方按「无清单」降级（前端允许手填并提示，不阻断）。
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
    items: list[dict] = []
    seen: set[tuple] = set()
    for page in pages:
        activity = str(page.get("activity", ""))
        for control in page.get("controls", []):
            rid = str(control.get("res-id", "") or "")
            label = str(control.get("text") or control.get("desc") or "").strip()
            if rid:
                if is_degenerate_res_id(rid) or ("rid", rid, label) in seen:
                    continue
                seen.add(("rid", rid, label))
                items.append({"res_id": rid, "label": label[:30],
                              "page": activity[:60], "clickable": bool(control.get("clickable"))})
            elif control.get("clickable") and label and not is_dynamic_text(label):
                if ("txt", label) in seen:
                    continue
                seen.add(("txt", label))
                items.append({"res_id": "", "label": label[:30],
                              "page": activity[:60], "clickable": True})
            if len(items) >= max_controls:
                break
        if len(items) >= max_controls:
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


def graph_context(pages: list[dict], max_pages: int = 12) -> str:
    """图谱页面 → 注入 prompt 的页面结构上下文（严格限量防 prompt 膨胀）。

    **取哪 12 页比取多少页更重要**（实测的三个缺陷，T54 修正）：
    - **按 identities 去重**：page_key 不稳会让同屏记成多条（真实 App 实测 131 页 ≈ 86 个功能），
      不去重时 12 个槽位里 3 个浪费在同一页；
    - **有 parent_action 的页优先**：没有它就没有「点[X] →」导航边，上下文退化成平铺标签
      （实测 79% 的页没有 parent_action ⇒ 默认排序下输出里一条导航边都没有）；
    - **depth 缺失不静默丢弃**：标 `L?`（曾用 `isinstance(depth, int)` 把 5 页静默滤掉）。
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
        parts.append(f"点[{page['parent_action']}] → {depth_txt} {suffix}"
                     if page.get("parent_action") else f"{depth_txt} {suffix}")
    return "【页面结构】" + " → ".join(parts)


def graph_context_for(package: str, base_dir: Path | str | None = None,
                      max_pages: int = 12) -> str:
    """从 `<base_dir>/<package>.json` 读图谱并产出页面结构上下文。

    默认**关闭**，`GEN_GRAPH_CONTEXT=1` 开启：这是会改变 AI 生成输出的增强，
    应先做真机 A/B 对照再决定是否默认打开（诚实边界）。
    """
    if os.environ.get("GEN_GRAPH_CONTEXT", "0").strip().lower() not in ("1", "true", "yes"):
        return ""
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
    return graph_context(index.get("pages", []), max_pages=max_pages)


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
            out.append({"bounds": node.bounds, "label": label})
        return out

    for _ in range(max_steps):
        xml = drv.dump()
        app = {}
        try:
            current = drv.foreground_app()
            app = {"package": current.get("package"), "activity": current.get("activity")}
        except Exception:
            pass
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
                      if (key, a.get("bounds", "")) not in tried]
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
        tried.add((key, target["bounds"]))
        tried_by_key.setdefault(key, set()).add(target["bounds"])
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
    """alt_entries 去重（按 父页 + 到达动作）。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        pair = (entry.get("parent_key"), entry.get("parent_action"))
        if pair in seen:
            continue
        seen.add(pair)
        out.append({"parent_key": pair[0], "parent_action": pair[1]})
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

    # depth 取更浅；父链随之更新为「更浅到达」的那条
    old_depth, new_depth = old.get("depth"), new.get("depth")
    if new_depth is not None and (old_depth is None or new_depth < old_depth):
        merged["depth"] = new_depth
        merged["parent_key"] = new.get("parent_key")
        merged["parent_action"] = new.get("parent_action")
    elif old_depth is not None:
        merged["depth"] = old_depth
    else:
        merged["depth"] = new_depth

    merged["first_seen"] = _earliest(old.get("first_seen"), new.get("first_seen"))
    merged["last_seen"] = _latest(old.get("last_seen"), new.get("last_seen"))
    merged["hits"] = int(old.get("hits", 0)) + int(new.get("hits", 0))
    merged["tried"] = sorted(set(old.get("tried") or []) | set(new.get("tried") or []))
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
