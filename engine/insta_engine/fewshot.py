"""同 App 历史成功用例的 few-shot 检索（L1，2026-10-28）。

**为什么是 few-shot 而不是微调**：数据在用户本机、场景窄；微调要 4–6 人天，且每次
prompt 改动都要重评。few-shot 零训练、随时更新，效果接近——尤其对"这个 App 的控件
习惯怎么写"这类知识，示例比权重更直接。

**数据源**：用户工作区的 `artifacts/**/run.json`。**只取 `run.status == "passed"`**——
没跑通的用例不进示例，否则等于把 AI 自己的错误也教回去（这是这条路最容易踩的坑）。

**开关**：`MAT_FEWSHOT=1` 显式开启（默认关）。默认关的理由：历史用例质量参差，
先在真实需求上对比，确认不劣化再改默认。条数由 `MAT_FEWSHOT_LIMIT` 控制（默认 3）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_EXAMPLES = 3
MAX_STEPS_PER_CASE = 8

#: 按工作区缓存索引（扫 2000+ 个 run.json 要几秒，不能每次生成都扫）。
_CACHE: dict = {"root": "", "index": {}}


def enabled() -> bool:
    return os.environ.get("MAT_FEWSHOT", "").strip().lower() in ("1", "true", "yes", "on")


def _limit() -> int:
    try:
        return max(0, min(5, int(os.environ.get("MAT_FEWSHOT_LIMIT", MAX_EXAMPLES))))
    except ValueError:
        return MAX_EXAMPLES


def _is_rhythm_only(step: dict) -> bool:
    """纯节奏步骤（wait 且无断言、无定位）：不进示例。

    2026-10-28 实测：示例里带 wait，模型会把 wait 节奏一起抄过来（内容社区应用 3 步变
    6 步、多 3 个 wait）。wait 是执行节奏不是定位知识——示例只该教"怎么写定位"。
    """
    action = str(step.get("action") or "")
    has_target = bool((step.get("target") or {}).get("value"))
    has_assert = bool(step.get("assertions"))
    return action == "wait" and not has_target and not has_assert


def _step_line(step: dict) -> str:
    """一步压成 `action(strategy=value)`；没有定位（launch）只留动作名。"""
    action = str(step.get("action") or "")
    target = step.get("target") or {}
    value = str(target.get("value") or "")
    strategy = str(target.get("strategy") or "")
    if not value:
        return action
    return f"{action}({strategy}={value})"


def _load_index(root: Path) -> dict[str, list[dict]]:
    key = str(root.resolve())
    if _CACHE.get("root") == key:
        return _CACHE["index"]

    index: dict[str, list[dict]] = {}
    for path in sorted(root.glob("artifacts/**/run.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run = data.get("run") or {}
        case = data.get("case") or {}
        if run.get("status") != "passed":  # 只收跑通的
            continue
        package = str((case.get("target") or {}).get("package") or "")
        steps = case.get("steps") or []
        if not package or not steps:
            continue
        index.setdefault(package, []).append({
            "name": str(case.get("name") or ""),
            "steps": steps,
            "finished_at": str(data.get("finished_at") or ""),
        })
    for items in index.values():
        items.sort(key=lambda c: c["finished_at"], reverse=True)

    _CACHE.update(root=key, index=index)
    return index


def _overlap(nl: str, name: str) -> int:
    """粗相似度：字符交集大小。中文没有空格，词重叠比编辑距离稳。"""
    return len(set(nl) & set(name))


def build(package: str, nl: str, root: Path | None = None) -> str:
    """返回可直接拼进 prompt 的示例段；无开关/无数据/无匹配时返回空串。"""
    if not enabled() or not package:
        return ""
    limit = _limit()
    if limit <= 0:
        return ""

    index = _load_index(root or Path.cwd())
    items = index.get(package) or []
    if not items:
        return ""

    picked = sorted(items, key=lambda c: _overlap(nl, c["name"]), reverse=True)[:limit]
    # 同名用例在不同 suite 跑过多次，去重后避免 3 条示例其实是同一条
    seen: set[str] = set()
    unique = []
    for case in picked:
        if case["name"] in seen:
            continue
        seen.add(case["name"])
        unique.append(case)

    # 2026-10-28 实测：只说"参考定位"不够——模型会把示例的步骤范围一起抄过来
    # （实测那条因此丢了"评分排序/进店"两步）。必须显式要求覆盖当前需求的全部动作。
    lines = ["【参考示例】本 App 已通过执行的历史用例，仅示范定位写法"
             "（尤其 res-id 的完整写法）。**不要照搬步骤范围、不要照抄目标、"
             "不要照搬 wait 节奏：当前需求的每个动作都必须体现在步骤里。**"]
    for i, case in enumerate(unique, 1):
        steps = [s for s in case["steps"] if not _is_rhythm_only(s)][:MAX_STEPS_PER_CASE]
        lines.append(f"{i}. {case['name']}")
        lines.append(f"   {' → '.join(_step_line(s) for s in steps)}")
    return "\n".join(lines)


def clear_cache() -> None:
    """测试用：清掉索引缓存。"""
    _CACHE.update(root="", index={})
