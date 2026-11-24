"""自愈提案回写（「确认自愈回写」动作的引擎侧实现）。

回写原则：默认仅提议；回写由用户显式触发（heal apply），不覆盖原用例。

支持两种自愈来源（2026-11-24 补全，评审 #6「回写不全」）：
- 本地模糊（`fuzzy:textContains` / `fuzzy:descriptionContains`）→ text / description；
- 图谱提示（`index-hints`，res-id 改名后用历史文案重定位）→ 命中的是 TEXT 策略，
  回写成 `text=历史文案`。此前它被当成「未知类型」跳过——最该自动回写的一类反而丢了。

视觉兜底（`vision-llm`）**不回写**：它给的是像素坐标，坐标随分辨率/布局变化失效，
写成 point 只会把不稳定的东西固化进用例。这种情况明确说明原因并建议人工改。
"""

from __future__ import annotations

import json
from pathlib import Path

from .dsl import Locator, LocatorStrategy, TestCase

_STRATEGY_MAP = {
    # contains 语义必须原样保留：映射成精确 `text` 会让回写后的用例失效——
    # 实测 `fuzzy:textContains=登录` 是匹配到「立即登录」的，写成 `text=登录` 就再也点不中。
    "fuzzy:textContains": LocatorStrategy.TEXT_CONTAINS,
    # description 没有对应的 contains 策略，退回精确（DSL 未提供 descriptionContains）
    "fuzzy:descriptionContains": LocatorStrategy.DESCRIPTION,
    # 图谱提示：`heal_by_hints` 走 TEXT 策略，match 形如 `text=登录`。
    "text": LocatorStrategy.TEXT,
    "description": LocatorStrategy.DESCRIPTION,
}

#: 明确"不能自动回写"的来源（不是"不认识"，是"认识了但不该写"）。
_NO_REWRITE_REASON = {
    "vision-llm": "视觉兜底给的是像素坐标，坐标会随分辨率/布局失效；"
                  "建议人工改成该控件的 text/description 后再替换",
}


def collect_proposals(run_dir: Path) -> list[dict]:
    """从执行目录的 run.json 收集自愈提案。

    P1-2（2026-10-24 评审）：run.json 是磁盘产物、可能被手改/损坏——此前裸
    `read_text + json.loads`，CLI 直接 traceback、web 端 500。这里把不可读/损坏
    翻译成**可行动**的 ValueError（与 apply_heals 对脏提案的防御同一纪律）。
    """
    run_json = Path(run_dir) / "run.json"
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(
            f"run.json 不可读：{run_json}（{exc}）——重跑该用例生成新产物") from exc
    except ValueError as exc:
        raise ValueError(
            f"run.json 已损坏：{run_json}（{exc}）——文件可能被手改，重跑该用例生成新产物"
        ) from exc
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
        if match_strategy in _NO_REWRITE_REASON:
            notes.append(f"{proposal.get('step', '?')}: 跳过回写——{_NO_REWRITE_REASON[match_strategy]}")
            return loc
        new_strategy = _STRATEGY_MAP.get(match_strategy)
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
