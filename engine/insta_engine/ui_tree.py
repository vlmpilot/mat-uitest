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
