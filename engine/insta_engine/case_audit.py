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

from .case_quality import fmt_issue
from .dsl import Action, LocatorStrategy, TestCase
from .page_index import controls_list_for

#: L6（R19）：系统/首启权限弹窗的常见按钮词（全文精确匹配，避免「同意条款」类误报）
_PERMISSION_WORDS = frozenset({
    "同意", "拒绝", "允许", "不允许", "始终允许", "仅本次允许",
    "仅在使用中允许", "禁止", "不再询问", "始终禁止",
})
_STATEFUL_TITLE_RE = re.compile(r"权限|授权|弹窗")

_TEXT_LIKE = (LocatorStrategy.TEXT, LocatorStrategy.TEXT_CONTAINS, LocatorStrategy.DESCRIPTION)
_ID_LIKE = (LocatorStrategy.RES_ID, LocatorStrategy.ID)


def audit_case(case: TestCase, index_dir: Path | None = None) -> list[str]:
    """对单条用例跑 L1/L6 审计，返回**带规则编号**的警告列表（只警告不阻断）。

    `index_dir`：page_index 目录（`root/artifacts/page_index`）。缺省 None 或目录里
    没有该包的索引时，L1 自动跳过（见模块说明），L6 为纯规则不依赖索引。
    """
    pkg = (case.target or {}).get("package", "")
    known_res: set[str] = set()
    list_texts: set[str] = set()
    if index_dir is not None and pkg:
        items = controls_list_for(pkg, index_dir)
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
    return warns
