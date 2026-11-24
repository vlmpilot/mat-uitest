"""探索路径 → 可回放用例（P1-2，2026-10-19）。

**解决什么**：explore 花真机预算走出来的路径此前只是「报告里的图」——看完就完了。
promote 把任意一条观察到的路径**落成 `cases/*.json`**，`mat run` 直接回放：
探索预算变成回归资产，这是「探索 → 用例」闭环的最后一公里（实施方案 §3.6）。

数据源与语义（与 explore.py 的落盘结构一一对齐）：

- `pages`：父链（parent_action/parent_rid）是 explore 已经收敛好的**最短可回放树**
  （`_record` 换链判据与 `hop_replayable` 同源）；`alt_entries` 是其余观察到的入口；
- `edges`：本次观察到的全部跳转 `{from, to, action, count}`——路径枚举的邻接表；
- 每跳的点击目标定位器：**优先 res-id**（改版最稳），否则 `text=到达动作文案`
  （判据 `hop_replayable`——iconfont 字符与内容型长文案写不出重放序列，三处共用
  同一函数防口径漂移）；文案在**来源页控件**里能按 bounds 找到 → 映射为
  `point` 坐标兜底（纯坐标点击，用例 notes 标「需人工确认」）；
- `BACK_HOP`（系统返回）映射为 `back` 动作（page_index 的可重放语义同源）。

边界（刻意收窄，防范围膨胀）：

- 只生成「launch + tap/back 序列」——**不生成断言**：探索不知道业务预期，
  断言该由人在确认路径价值后补（或走 P1-1 的 api 步骤）；
- 路径枚举是**简单路径**（页面不重复）：探索图可能带环，回放最忌自己转圈；
- 不支持 hop（无 rid、文案不可重放、控件也找不到）→ 在该跳**截断**并记 notes：
  后续跳的起点已经不成立，硬拼只会产出必挂用例。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .dsl import Action, Assertion, AssertionType, Locator, LocatorStrategy, Step, TestCase
from .page_index import BACK_HOP, hop_replayable, is_dynamic_text

#: 路径枚举的最大跳数（防深图组合爆炸；探索默认深度 3–4，8 已宽裕）
MAX_HOPS = 8
#: 路径枚举的总数上限（list 输出与 --path 引用的稳定上界）
MAX_PATHS = 200

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


@dataclass
class Hop:
    """一条跳转：from 页 → to 页，点击目标 = label/rid（与 explore 的观测字段同名）。"""

    from_key: str
    to_key: str
    label: str  # 到达动作文案（edges.action / pages.parent_action）；BACK_HOP = 系统返回
    rid: str  # 控件 resource-id（可能为空）
    count: int = 1  # 该边被观察到的次数（多次探索累积的置信度代理）


@dataclass
class PathInfo:
    """一条可提升路径（简单路径：页面不重复）。"""

    index: int
    hops: list[Hop]
    pages: list[str] = field(default_factory=list)  # 页链（from...to 的 page_key）
    end_activity: str = ""
    end_title: str = ""
    replayable: bool = True  # 全跳都有定位通道（rid/文案/坐标三者其一）

    def summary(self) -> str:
        labels = " › ".join(h.label if h.label != BACK_HOP else "返回" for h in self.hops)
        end = self.end_title or self.end_activity or self.pages[-1] if self.pages else ""
        return f"#{self.index}  {len(self.hops)} 跳  {labels}  → {end}"


def load_explore(path: Path) -> dict:
    """读取 explore.json；结构不对就给出人话报错（ValueError）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"explore 文件读取失败: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"explore 文件不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        raise ValueError("不是 explore.json（缺 pages 列表）——请传 artifacts/explore_<id>/explore.json")
    return data


def _page_map(explore: dict) -> dict[str, dict]:
    return {p.get("page_key"): p for p in explore.get("pages") or [] if p.get("page_key")}


def _rid_for_edge(pages: dict[str, dict], hop_from: str, label: str, hop_to: str) -> str:
    """从子页的父链 / alt_entries 里找回该跳的控件 rid（explore 边聚合时丢了 rid，这里找回）。"""
    child = pages.get(hop_to) or {}
    if child.get("parent_key") == hop_from and (child.get("parent_action") or "") == label:
        return str(child.get("parent_rid") or "")
    for entry in child.get("alt_entries") or []:
        if entry.get("parent_key") == hop_from and (entry.get("parent_action") or "") == label:
            return str(entry.get("parent_rid") or "")
    return ""


def _edges_of(explore: dict) -> list[Hop]:
    pages = _page_map(explore)
    hops: list[Hop] = []
    for e in explore.get("edges") or []:
        src, dst, label = str(e.get("from") or ""), str(e.get("to") or ""), str(e.get("action") or "")
        if not src or not dst:
            continue
        hops.append(Hop(src, dst, label, _rid_for_edge(pages, src, label, dst),
                        int(e.get("count") or 1)))
    return hops


def _roots(pages: dict[str, dict]) -> list[str]:
    """探索起始页（depth 0 / 无父链）。多个根都枚举（多入口 App 常见）。"""
    roots = [k for k, p in pages.items()
             if not p.get("parent_key") or int(p.get("depth") or 0) == 0]
    return sorted(roots) or sorted(pages)


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    m = _BOUNDS_RE.match(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _point_for_label(source_page: dict, label: str) -> tuple[int, int] | None:
    """在来源页控件里按文案找回点击坐标（纯坐标兜底；优先 clickable 控件）。"""
    hits = []
    for c in source_page.get("controls") or []:
        text = (c.get("text") or c.get("desc") or "").strip()
        if text == label and c.get("bounds"):
            center = _bounds_center(c["bounds"])
            if center:
                hits.append((bool(c.get("clickable")), center))
    if not hits:
        return None
    hits.sort(key=lambda t: not t[0])  # clickable 优先
    return hits[0][1]


def list_paths(explore: dict, max_hops: int = MAX_HOPS) -> list[PathInfo]:
    """按**导航栈语义**枚举可回放路径：tap 只去栈外页，back 只回栈内祖先。

    为什么不是图论的简单路径：back 边必然指向已访问页，简单路径会把系统返回
    全部排除——而「下钻→返回→换分支」恰恰是真实回放的主形态（与 explore 的
    导航栈同构）。防环用两条规则：tap 的目标页不得在当前栈上；同一条 tap 边
    （from,label,to）在单条路径内不得重复使用（杜绝「点设置→返回→再点设置」）。
    只在叶子（无前向无返回可走）或深度上限处记录路径——前缀不单列。
    """
    pages = _page_map(explore)
    adj: dict[str, list[Hop]] = {}
    for hop in _edges_of(explore):
        adj.setdefault(hop.from_key, []).append(hop)

    found: list[tuple[list[str], list[Hop]]] = []

    def walk(node: str, stack: list[str], chain: list[Hop], used: set[tuple[str, str, str]]) -> None:
        if len(found) >= MAX_PATHS:
            return
        forward = sorted(
            (h for h in adj.get(node, [])
             if h.label != BACK_HOP and h.to_key not in stack
             and (h.from_key, h.label, h.to_key) not in used),
            key=lambda h: (-h.count, h.label))
        backs = sorted(
            (h for h in adj.get(node, [])
             if h.label == BACK_HOP and h.to_key in stack),
            key=lambda h: -h.count)
        if not forward and not backs:
            if chain:
                found.append((list(stack), list(chain)))  # 叶子：栈快照即页链（栈天然无重复）
            return
        if len(chain) >= max_hops:
            found.append((list(stack), list(chain)))  # 深度上限：就地截断记录
            return
        for hop in forward:
            walk(hop.to_key, stack + [hop.to_key], chain + [hop],
                 used | {(hop.from_key, hop.label, hop.to_key)})
            if len(found) >= MAX_PATHS:
                return
        for hop in backs:
            target_idx = stack.index(hop.to_key)
            walk(hop.to_key, stack[:target_idx + 1], chain + [hop], used)
            if len(found) >= MAX_PATHS:
                return

    for root in _roots(pages):
        if len(found) >= MAX_PATHS:
            break
        walk(root, [root], [], set())

    infos: list[PathInfo] = []
    for i, (chain_pages, chain_hops) in enumerate(found):
        end = pages.get(chain_pages[-1]) or {}
        replayable = all(
            hop.label == BACK_HOP or hop_replayable(hop.label, hop.rid)
            for hop in chain_hops)
        infos.append(PathInfo(index=i, hops=chain_hops, pages=chain_pages,
                              end_activity=str(end.get("activity") or ""),
                              end_title=str(end.get("title") or ""),
                              replayable=replayable))
    return infos


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_\-]+", "-", (text or "").strip()).strip("-")
    return cleaned or "explore-case"


#: 到达断言候选的最大长度：再长大概率是内容标题（跨运行会变），当断言就会假红。
_ARRIVAL_LABEL_MAX = 12

#: **混淆过的资源 id**：`0_resource_name_obfuscated` 这类由构建工具生成，
#: 换一次加固/混淆策略就变 ⇒ 拿它当断言等于埋一颗定时假红。实测于某内容社区 App。
_OBFUSCATED_RID = re.compile(r"obfuscated|:id/0_[0-9a-f_]+$", re.IGNORECASE)

#: 结构性描述（容器/区域/布局）：它们是**页面骨架**，不是"到了目标页"的证据，
#: 而且换版本时最容易改名。实测 `头图or头视频区容器` 就是被 key_elements 收进来的。
_STRUCTURAL_LABEL = re.compile(r"容器|区域|布局|占位|container|layout|wrapper", re.IGNORECASE)


def arrival_assertion(target_page: dict | None,
                      start_page: dict | None = None) -> tuple[Assertion | None, str]:
    """给「到达目标页」挑一条**有区分度**的断言（评审 #13：转出的用例此前没有任何断言）。

    ## 判据（与 R24「动作前已成立」同一思路）

    | 过滤 | 为什么 |
    |---|---|
    | 起点页也出现 | 恒真，证明不了"到了新页" |
    | 动态文本（含数字/时间） | 跨运行会变，断言必然假红 |
    | 超过 12 字 | 大概率是内容标题，同上 |
    | 空文案 | 定位不了 |

    优先 `res-id`：同一文案能在目标页控件里对到 rid 时用它——**文案会改，id 不改**。
    选的是**最靠前**的一条（自顶向下），因为它通常是页面标题/首个特征控件，最稳定。
    """
    if not target_page:
        return None, "目标页结构缺失（探索未记录该页）"
    target_labels = [str(x).strip() for x in
                     ((target_page.get("structure") or {}).get("key_elements") or [])]
    start_labels = {str(x).strip() for x in
                    ((start_page or {}).get("structure") or {}).get("key_elements") or []}
    if not target_labels:
        return None, "目标页没有可用的特征文案（key_elements 为空）"

    controls = target_page.get("controls") or []
    for label in target_labels:
        if not label or len(label) > _ARRIVAL_LABEL_MAX:
            continue
        if label in start_labels:      # 起点页也有 ⇒ 无区分度
            continue
        if is_dynamic_text(label):     # 动态/含数字 ⇒ 跨运行会变
            continue
        if _STRUCTURAL_LABEL.search(label):  # 容器/区域描述 ⇒ 骨架，不是内容证据
            continue
        rid = next((str(c.get("res-id") or "").strip() for c in controls
                    if str(c.get("res-id") or "").strip()
                    and label in (str(c.get("text") or ""), str(c.get("desc") or ""))), "")
        if rid and _OBFUSCATED_RID.search(rid):
            # 混淆 id 不用，退到文案：它至少是人能看懂、随版本可核对的东西
            rid = ""
        if rid:
            return (Assertion(type=AssertionType.EXISTS,
                              target=Locator(strategy=LocatorStrategy.RES_ID, value=rid)),
                    f"到达断言用 res-id({rid})——文案「{label}」能对上控件，id 比文案稳")
        return (Assertion(type=AssertionType.EXISTS,
                          target=Locator(strategy=LocatorStrategy.TEXT, value=label)),
                f"到达断言用文案「{label}」——该页有、起点页没有")
    return None, ("目标页与起点页没有可区分的稳定文案——"
                  "该用例**证明不了**「到了目标页」，建议人工补一条断言")


def promote_path(explore: dict, index: int, name: str | None = None,
                 case_id: str | None = None,
                 max_hops: int = MAX_HOPS) -> tuple[TestCase, list[str]]:
    """把 list_paths 编号为 index 的路径落成 TestCase；返回 (用例, notes)。

    max_hops 必须与枚举时一致（CLI 同传 --max-depth），否则编号会错位。
    notes 记录所有「需人工确认」的事项（坐标兜底跳/截断跳/回放起点假设），
    一并写进 meta——控制台与报告可见（方案验收：生成物带 source 与 notes）。
    """
    paths = list_paths(explore, max_hops=max_hops)
    if not paths:
        raise ValueError("explore.json 里没有可提升的路径（pages/edges 为空或探索一轮无跳转）")
    if index < 0 or index >= len(paths):
        raise ValueError(f"路径编号 {index} 不存在（--list 可见 0–{len(paths) - 1}）")
    path = paths[index]

    package = str(explore.get("package") or "")
    if not package:
        raise ValueError("explore.json 缺 package 字段，无法写 launch 步骤")
    pages = _page_map(explore)
    notes: list[str] = []
    steps: list[Step] = []

    steps.append(Step(id="s1", intent="启动 App（探索回放假设：冷启动后停在探索起始页）",
                      action=Action.LAUNCH,
                      params={"package": package, "cold": True}))

    current = path.pages[0]
    for n, hop in enumerate(path.hops, start=2):
        if hop.label == BACK_HOP:
            steps.append(Step(id=f"s{n}", intent=f"系统返回（探索观测：{hop.from_key[:24]}…→{hop.to_key[:24]}…）",
                              action=Action.BACK))
            current = hop.to_key
            continue
        if hop.rid:
            target = Locator(strategy=LocatorStrategy.RES_ID, value=hop.rid)
        elif hop_replayable(hop.label, hop.rid) and hop.label:
            target = Locator(strategy=LocatorStrategy.TEXT, value=hop.label)
        else:
            center = _point_for_label(pages.get(current) or {}, hop.label)
            if center is None:
                notes.append(
                    f"第 {n - 1} 跳「{hop.label or hop.rid or '<空>'}」不可重放"
                    f"（无 rid、文案不可重放、来源页控件也找不到）——用例在此**截断**；"
                    f"请人工确认该跳的真实控件后补一步 tap")
                break
            target = Locator(strategy=LocatorStrategy.POINT, value=f"{center[0]},{center[1]}")
            notes.append(
                f"第 {n - 1} 跳「{hop.label}」按探索时的控件位置落成**纯坐标点击**"
                f"（{center[0]},{center[1]}）——分辨率/布局变化会失效，需人工确认后改为稳定定位器")
        steps.append(Step(id=f"s{n}", intent=f"点击「{hop.label or hop.rid}」（探索观测路径）",
                          action=Action.TAP, target=target))
        current = hop.to_key

    # 到达断言（评审 #13）：给最后一步补一条能证明「到了目标页」的断言。
    # 没有断言的用例**可以回放但不可判定**——它会占着"通过"却什么也没证明，
    # 与本项目"verify 数字要可信"直接冲突。
    # 最后一步是系统返回时跳过：回到起点页不算"到达"，那条断言没有语义。
    last_hop = path.hops[-1] if path.hops else None
    if steps and last_hop is not None and last_hop.label != BACK_HOP:
        arrival, why = arrival_assertion(pages.get(path.pages[-1]), pages.get(path.pages[0]))
        if arrival is not None:
            steps[-1].assertions = [arrival]
        notes.append(why)

    name = _slug(name or f"explore-{_slug(explore.get('explore_id') or 'case')}-{index:02d}")
    case_id = _slug(case_id or name)
    meta = {"source": "explore", "explore_id": str(explore.get("explore_id") or ""),
            "path_pages": path.pages, "notes": notes,
            "hops": [{"from": h.from_key, "to": h.to_key, "label": h.label, "rid": h.rid,
                      "count": h.count} for h in path.hops]}
    return TestCase(
        case_id=case_id,
        name=name,
        target={"package": package},
        steps=steps,
        meta=meta,
        preconditions=["回放起点假设：冷启动后停留在探索起始页"
                       "（App 有开屏广告/系统弹窗时先人工处理，runner 会自动清理常见噪声）"],
    ), notes


def save_case(case: TestCase, out: Path) -> Path:
    """用例落盘（cases/*.json）；目录不存在自动创建。"""
    from .dsl import dump_dsl

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump_dsl(case), encoding="utf-8")
    return out
