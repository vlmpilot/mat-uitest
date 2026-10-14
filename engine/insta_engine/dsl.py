"""步骤 DSL 的权威数据模型（与 JSON Schema 同源）。

自然语言生成、手动编辑、录制回放三源归一于本模型；benchmark gold 用例亦按此标注。
"""

from __future__ import annotations

import json
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LocatorStrategy(StrEnum):
    RES_ID = "res-id"
    ID = "id" # 平台中立静态标识：Android=res-id 同义，iOS=accessibility identifier
    TEXT = "text"
    TEXT_CONTAINS = "textContains"  # u2 原生 textContains；v1.1 加入（结果页/列表类断言定位）
    DESCRIPTION = "description"  # content-desc
    XPATH = "xpath"
    IMAGE_TEMPLATE = "image-template"  # V1：引擎路线图
    VISION_LLM = "vision-llm"  # 轻云服务提供（闭源边界）
    POINT = "point"


VISION_LLM_MISUSE_HINT = (
    "vision-llm 不能在 DSL 定位链里声明：它由**自愈兜底**触发，且按次计费、默认关闭。"
    "正确用法：步骤标 \"onFailure\": \"heal\"，或设环境变量 GEN_VL_FALLBACK=1"
)


class Locator(BaseModel):
    """元素定位；fallback 链按成本递增逐级降级（02 §3.2 策略链）。"""

    strategy: LocatorStrategy
    value: str
    fallback: Locator | None = None

    @model_validator(mode="after")
    def _reject_vision_llm(self) -> Locator:
        """解析期就拒绝 vision-llm（fail fast）。

        理由同 locator._try：它在定位链里是**静默空操作**——写了既不生效也不报错。
        fallback 链的每一级都是独立 Locator，所以逐级各自校验即可覆盖整条链。
        """
        if self.strategy == LocatorStrategy.VISION_LLM:
            raise ValueError(VISION_LLM_MISUSE_HINT)
        return self


class AssertionType(StrEnum):
    EXISTS = "exists"
    NOT_EXISTS = "not-exists"
    TEXT_EQUALS = "text-equals"
    TEXT_CONTAINS = "text-contains"
    ENABLED = "enabled"
    DISABLED = "disabled"
    CHECKED = "checked"
    SELECTED = "selected"


#: **不使用 `expected` 的断言类型**（T56）。模型经常给它们带上 `expected`——
#: 实测最典型的是 `{"type": "exists", ..., "expected": true}`。该字段对这些类型
#: **没有语义**，但 schema 要求它是字符串 ⇒ 一个良性笔误就让**整次生成作废**
#: （实测生成失败率 39%）。
_NO_EXPECTED = frozenset({
    AssertionType.EXISTS, AssertionType.NOT_EXISTS,
    AssertionType.ENABLED, AssertionType.DISABLED,
    AssertionType.CHECKED, AssertionType.SELECTED,
})


class Assertion(BaseModel):
    type: AssertionType
    target: Locator | None = None
    expected: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _tolerate_expected(cls, data):
        """宽容处理 `expected`（T56）：**丢无用的 / 转非字符串的**，而不是拒绝整个用例。

        两条规则：
        - 该类型**本来就不用** `expected`（`exists` / `not-exists` / 状态类）⇒ **丢弃**该字段；
        - 该类型**确实用**（`text-equals` / `text-contains`）但模型给了非字符串
          （bool / 数字）⇒ `str()` 转换。

        为什么是"宽容"而不是"报错"：生成物**本来就要人工校对**（见 `ai_client.generate`
        的 docstring）。让用例生成出来、人工看一眼，比整次生成失败有用得多。
        """
        if not isinstance(data, dict):
            return data
        if data.get("type") in _NO_EXPECTED:
            return {k: v for k, v in data.items() if k != "expected"}
        exp = data.get("expected")
        if exp is not None and not isinstance(exp, str):
            return {**data, "expected": str(exp)}
        return data


class Action(StrEnum):
    LAUNCH = "launch"
    TAP = "tap"
    LONG_PRESS = "long-press"
    INPUT = "input"
    CLEAR = "clear"
    SWIPE = "swipe"
    SCROLL = "scroll"
    BACK = "back"
    HOME = "home"
    KEY_EVENT = "key-event"
    WAIT = "wait"


# DSL JSON 规范为 camelCase，Python 字段为 snake_case；
# populate_by_name 允许两种命名入参，序列化统一走 dump_dsl（by_alias）。
class OnFailure(StrEnum):
    ABORT = "abort"
    RETRY = "retry"
    HEAL = "heal"  # 触发自愈管线（本地模糊 → vision-llm）
    # 可选步骤（2026-10-13）：启动广告/权限弹窗/引导浮层类前置噪声步骤——目标没出现
    # 是正常态而非失败（实测 4 用例全栽在「跳过启动广告」但广告已消失）。定位失败
    # ⇒ 记 skipped 继续；其余异常仍按 failed 处理但不中止后续。
    CONTINUE = "continue"


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    intent: str = ""  # 中文意图，报告与编辑展示用
    action: Action
    target: Locator | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    #: C6（2026-10-12 评审）：无范围约束曾被实测穿透 —— `timeoutMs: -5` 通过校验且整步
    #: `passed`（deadline 立即过期 → 等待被压到下限，断言被静默削弱）；`999999999` 可挂起
    #: 277 小时烧穿真机预算。上限取"最长合理单步"（视觉兜底 + 多策略定位 + 重试）。
    timeout_ms: int = Field(default=8000, ge=100, le=120000, alias="timeoutMs")
    on_failure: OnFailure = Field(default=OnFailure.ABORT, alias="onFailure")  # heal 由自愈管线实现
    source_step: int | None = Field(default=None, alias="sourceStep")  # 导入溯源：IR 步骤序号
    assertions: list[Assertion] = Field(default_factory=list)


class TestCase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    case_id: str = Field(alias="caseId")
    name: str = ""
    target: dict[str, str] = Field(default_factory=dict)  # {"package": ..., "app_id": ...}
    vars: dict[str, str] = Field(default_factory=dict)  # ${变量名} 注入，凭据由测试数据管理注入
    meta: dict[str, Any] = Field(default_factory=dict)  # module/priority/tags/external_id/source（导入溯源）
    #: 手工用例的「前置条件」原样带过来。导入器的 IR 里有这个字段，但**此前 DSL 没有** ⇒
    #: 在边界上被静默丢弃（实测：某资讯类客户端 16 条手工用例全成了 `launch + 1~2 动作`，却去点别的
    #: 页面才有的控件 ⇒ 7 条假的"定位失败"）。它不是步骤，而是"跑之前设备该处于什么状态"，
    #: 所以运行期要**可见**（打印 / 报告 / run.json），无法置备的要**拒跑** —— 见
    #: `preconditions.py` 的模块说明。
    preconditions: list[str] = Field(default_factory=list)
    datasets: list[dict[str, Any]] | None = None  # 数据驱动（CASE-06）：[{"name": "组名", "vars": {...}}]
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_shape(self) -> TestCase:
        # A3 同口径：target.package 是驱动设备 + 拼 artifacts 路径的用户输入，
        # 形状非法（含路径分隔符 / 绝对路径）在边界上就拒绝，不给越界写原语。
        pkg = (self.target or {}).get("package") or ""
        if pkg:
            from .pkgname import safe_package_name

            try:
                safe_package_name(pkg)
            except ValueError as exc:
                raise ValueError(f"target.package {exc}") from exc
        # 重复 step id 会让重试时截图/证据互相覆盖 ——
        # `step_02_<id>.png` 同名文件，第二次跑覆盖第一次的失败现场。
        seen: set[str] = set()
        for st in self.steps:
            if st.id in seen:
                raise ValueError(f"steps 中存在重复的 step id: {st.id!r}（重试截图会互相覆盖，请保证唯一）")
            seen.add(st.id)
        return self


def unique_case_id(case_id: str, cases_dir: Path) -> str:
    """保证 caseId 在工作区唯一（模型可能重复生成同一后缀，2026-09-27 实测）。"""
    existing = set()
    if cases_dir.exists():
        for p in cases_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("caseId"):
                    existing.add(data["caseId"])
            except (OSError, ValueError):
                continue
    if case_id not in existing:
        return case_id
    base = case_id.rsplit("_", 1)[0]
    while True:
        candidate = f"{base}_{uuid.uuid4().hex[:6]}"
        if candidate not in existing:
            return candidate


def dump_dsl(case: TestCase) -> str:
    """按 DSL 规范（camelCase）序列化；写文件与回喂模型一律使用本函数。"""
    return case.model_dump_json(indent=2, by_alias=True)


Locator.model_rebuild()
