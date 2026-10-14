"""自愈提案回写（「确认自愈回写」动作的引擎侧实现）。

回写原则：默认仅提议；回写由用户显式触发（heal apply），不覆盖原用例。
v0 仅支持本地模糊线索回写（textContains/descriptionContains → text/description 精确定位），
云端视觉自愈（vision-llm）落地后按同一机制回写并附截图证据。
"""

from __future__ import annotations

import json
from pathlib import Path

from .dsl import Locator, LocatorStrategy, TestCase

_FUZZY_MAP = {
    "fuzzy:textContains": LocatorStrategy.TEXT,
    "fuzzy:descriptionContains": LocatorStrategy.DESCRIPTION,
}


def collect_proposals(run_dir: Path) -> list[dict]:
    """从执行目录的 run.json 收集自愈提案。"""
    data = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    proposals = []
    for step in data.get("run", {}).get("steps", []):
        if step.get("heal"):
            proposals.append({"step": step.get("step_id"), **step["heal"]})
    return proposals


def apply_heals(case: TestCase, proposals: list[dict]) -> tuple[TestCase, list[str]]:
    """把提案应用为新的用例对象（不落盘、不改原对象）；返回 (新用例, 变更说明列表)。"""
    # run.json 是磁盘产物，可能被手改/损坏——缺 original/match 键的提案跳过，
    # 不能让一条脏数据把整个回写炸成 KeyError（2026-10-12 评审）。
    lookup = {p["original"]: p for p in proposals
              if isinstance(p, dict) and p.get("original") and p.get("match")}
    notes: list[str] = []

    def replace(loc: Locator | None) -> Locator | None:
        if loc is None:
            return None
        key = f"{loc.strategy.value}={loc.value}"
        proposal = lookup.get(key)
        if not proposal:
            return loc
        match_strategy, _, match_value = str(proposal["match"]).partition("=")
        new_strategy = _FUZZY_MAP.get(match_strategy)
        if new_strategy is None:
            notes.append(f"{proposal.get('step', '?')}: 未知自愈匹配类型 {match_strategy}，跳过回写")
            return loc
        notes.append(
            f"{proposal.get('step', '?')}: {key} → {new_strategy.value}={match_value}（模糊线索回写，建议真机复核）"
        )
        return Locator(strategy=new_strategy, value=match_value, fallback=loc.fallback)

    new_case = case.model_copy(deep=True)
    for step in new_case.steps:
        step.target = replace(step.target)
        for assertion in step.assertions:
            assertion.target = replace(assertion.target)
    return new_case, notes
