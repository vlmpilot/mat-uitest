"""步骤覆盖自检：需求里的每个动作，生成结果是否都有步骤承载（R25）。

**动机**（2026-10-28 实测）：few-shot 会把示例的步骤范围带过来——实测用例因此丢了
「评分排序 / 进店」两步。漏步骤比定位错更危险：定位错会失败，步骤少会"跑完还报通过"。

**做法**：把需求按标点切成子句，用字符覆盖率判断每个子句是否有步骤承载。
不用动词词表：中文动作表达太散（"进店" vs "点击店铺" vs "进入评分最高的一家店"），
词表一定漏；字符覆盖对这类改写更稳。

**只报不改**（与 `case_quality` 同哲学）：覆盖率是启发式，误判时不该阻断生成。
"""

from __future__ import annotations

import os
import re

#: 子句与步骤文本的字符覆盖率下限。低于此值视为"没有步骤承载这个动作"。
COVERAGE_MIN = 0.45

_SPLIT = re.compile(r"[，,。；;、]")
#: 算覆盖率前剔除非文字字符。书名号/引号会稀释分母（实测「搜索「火锅」」被误判成 33%）。
_CLEAN = re.compile(r"[^\u4e00-\u9fffa-zA-Z0-9]")
#: 切分后不构成"动作"的碎片（连接词/语气词）。
_NOISE = re.compile(r"^(然后|接着|再|并|最后|之后|并且)$")


def retry_enabled() -> bool:
    """缺步骤时是否自动补（`MAT_COVERAGE_RETRY=1`）。默认只报不改。"""
    return os.environ.get("MAT_COVERAGE_RETRY", "").strip().lower() in (
        "1", "true", "yes", "on")


def clauses(nl: str) -> list[str]:
    """把需求切成动作子句；连接词碎片丢掉。"""
    return [c.strip() for c in _SPLIT.split(nl)
            if c.strip() and not _NOISE.match(c.strip())]


def _target_value(step: object) -> str:
    target = step.get("target") if isinstance(step, dict) else getattr(step, "target", None)
    if target is None:
        return ""
    if isinstance(target, dict):
        return str(target.get("value") or "")
    return str(getattr(target, "value", "") or "")


def _step_text(step: object) -> str:
    intent = step.get("intent") if isinstance(step, dict) else getattr(step, "intent", "")
    return f"{intent or ''} {_target_value(step)}".strip()


def _chars(text: str) -> set[str]:
    return set(_CLEAN.sub("", text))


def _coverage(clause: str, texts: list[str]) -> float:
    chars = _chars(clause)
    if not chars:
        return 0.0
    return max((len(chars & _chars(t)) / len(chars) for t in texts), default=0.0)


def missing(nl: str, case: object) -> list[str]:
    """返回未被任何步骤覆盖的需求子句（空列表 = 覆盖完整）。

    `case` 可以是 `TestCase` 或 `dict`（raw DSL）——生成路径与测试两边都能用。
    """
    steps = getattr(case, "steps", None)
    if steps is None and isinstance(case, dict):
        steps = case.get("steps") or []
    texts = [_step_text(s) for s in (steps or [])]

    issues: list[str] = []
    for clause in clauses(nl):
        coverage = _coverage(clause, texts)
        if coverage < COVERAGE_MIN:
            issues.append(
                f"[R28] 需求片段「{clause}」没有对应步骤（最高字符覆盖 {coverage:.0%}）"
                "——漏步骤会让用例跑完仍报通过")
    return issues
