"""a11y XML 解析的单一实现（原先散在 4 处，收敛于此以免口径漂移）。

所有需要"从 dump_hierarchy 输出里取控件"的模块（page_index/explore/web_server/runner）
统一走 parse_nodes，再各自施加过滤谓词——同一份 XML 在任何入口得到同一元素集合。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NODE_RE = re.compile(r"<node[^>]*/?>")
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


@dataclass(frozen=True)
class Node:
    cls: str
    text: str
    desc: str
    rid: str
    clickable: bool
    package: str
    bounds: str

    @property
    def label(self) -> str:
        """可见语义文案（text 优先，desc 兜底）。"""
        return self.text or self.desc

    @property
    def center(self) -> tuple[int, int] | None:
        match = _BOUNDS_RE.match(self.bounds)
        if not match:
            return None
        x1, y1, x2, y2 = (int(g) for g in match.groups())
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    def in_package(self, package: str) -> bool:
        """package 为空串时不过滤（被测包外的系统窗口由调用方决定去留）。"""
        return not package or self.package in ("", package)


def parse_nodes(xml: str) -> list[Node]:
    return [
        Node(
            cls=attrs.get("class", ""),
            text=attrs.get("text", ""),
            desc=attrs.get("content-desc", ""),
            rid=attrs.get("resource-id", ""),
            clickable=attrs.get("clickable") == "true",
            package=attrs.get("package", ""),
            bounds=attrs.get("bounds", ""),
        )
        for attrs in (_ATTR_RE.findall(node) and dict(_ATTR_RE.findall(node)) for node in _NODE_RE.findall(xml))
    ]

def rect(node: Node) -> tuple[int, int, int, int] | None:
    """节点矩形 (x1, y1, x2, y2)；bounds 不合法返回 None。"""
    match = _BOUNDS_RE.match(node.bounds or "")
    return tuple(int(g) for g in match.groups()) if match else None  # type: ignore[return-value]


def clickable_anchor(node: Node, clickables: list[Node]) -> Node | None:
    """找包住 `node` 的**最小**可点节点（空间包含近似"可点祖先"）。

    为什么需要它（2026-10-21 实测）：现代 Android 的入口普遍是「可点容器 + 不可点文字子节点」
    （`RelativeLayout` 可点、里面的 `TextView` 不可点）。探索的候选生成要求**节点自己**
    `clickable`，于是这些**有文案的入口永远进不了候选** —— 频道 tab、筛选条、底部导航都点不到，
    探索只能在信息流和广告里打转（实测 tx-news 首页快照 81 个控件里**一个频道 tab 都没有**，
    `parent_action` 全是「天猫双11热卖中」「《某电商 App平台服务协议》」这类广告落地页）。

    `parse_nodes` 是**扁平**的（正则解析、无父子关系），所以用矩形包含关系近似：
    取面积最小（最贴近）的那个可点节点作为点击落点。
    """
    target = rect(node)
    if target is None:
        return None
    best: Node | None = None
    best_area: int | None = None
    for cand in clickables:
        box = rect(cand)
        if box is None:
            continue
        if box[0] <= target[0] and box[1] <= target[1] and box[2] >= target[2] and box[3] >= target[3]:
            area = (box[2] - box[0]) * (box[3] - box[1])
            if best_area is None or area < best_area:
                best, best_area = cand, area
    return best

