"""`mat.verify/1` 的 Pydantic 模型(协议 v4 §4.2)——schema 的单一来源。

`build_verdict` 的返回从裸 dict 收紧为模型(2026-10-31):此前部分 return 路径
漏 `queued/wait_ms` 导致消费方 KeyError(评审 P0-1)——`extra="forbid"` 之后
这类漏字段在**生成时**就报错,而不是等消费方炸。`.model_dump()` 出口不变。

⚠️ `extra="forbid"` 前必须枚举**所有**动态键(修订 9):`notes` 是可变追加、
`blocked_reason/blocked_preconditions/install/queued/wait_ms` 是部分分支写入、
批量模式的 `mode/summary/selection/cases` 是聚合层写入——全在下面。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class VerifyVerdict(BaseModel):
    """单条验证结果(mat.verify/1)。所有字段都有值——部分分支不写的键取默认,
    让"漏写"变成"显式默认"而不是"消费方 KeyError"。"""

    model_config = {"extra": "forbid"}

    schema_: Literal["mat.verify/1"] = Field(default="mat.verify/1", alias="schema")
    verdict: Literal["pass", "fail", "blocked", "incomplete", "error"]
    ok: bool
    exit_code: int
    # 排队语义(协议 v3 §3.3):默认 false/0——部分分支覆写真实值
    queued: bool = False
    wait_ms: int = 0
    case_id: str = ""
    case_name: str = ""
    status: str | None = None
    step_success_rate: float | None = None
    duration_ms: int | None = None
    steps: dict = Field(default_factory=dict)
    failed_categories: dict = Field(default_factory=dict)
    failed_steps: list[dict] = Field(default_factory=list)
    next_actions: list[dict] = Field(default_factory=list)
    preconditions: list = Field(default_factory=list)
    artifacts: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    verified_at: str | None = None
    # 部分分支写入的键(修订 9:动态键必须枚举)
    blocked_reason: Literal["device_busy", "precondition"] | None = None
    blocked_preconditions: list | None = None
    install: dict | None = None
    # 批量模式(§4.1)追加
    mode: str | None = None
    summary: dict | None = None
    selection: dict | None = None
    cases: list[dict] | None = None


def model_from(verdict: dict) -> VerifyVerdict:
    """dict → 模型(未知键会抛 ValidationError——这正是"枚举动态键"的测试点)。"""
    return VerifyVerdict.model_validate(verdict)
