"""LLM 失败归因（BYOK 双模式实现）。

定位：**规则归因永远保留作回退**——无 Key / 调用失败 / 输出非法时退回规则分类，
绝不允许因归因失败而中断执行或污染 run.json。本模块只做"加分项"：
在规则结果之上，用 LLM 输出四分类（app-bug/ui-change/env-flake/precondition）、
证据引用与修复建议；置信度 < 0.6 强制 need_human（错误成本不对称）。

双模式与 gen 完全一致：GEN_PROMPT_MODE=local（BYOK，prompts/diagnose.prompt）
或 =server（轻云调优版，计 1 次生成额度）。promptstore 未播种时 mat init 会播种
prompts/diagnose.prompt.example。
"""

from __future__ import annotations

import json
from pathlib import Path

from .report import _CATEGORY_LABELS
from .runner import RunResult

# LLM 四分类 → 报告展示标签
ATTR_LABELS = {
    "app-bug": "真 bug（App 缺陷）",
    "ui-change": "UI 改版（定位器过期）",
    "env-flake": "环境抖动",
    "precondition": "前置条件缺失",
}
CONFIDENCE_FLOOR = 0.6  # 低于此值一律 need_human（错误成本不对称）


def _steps_summary(run: RunResult) -> str:
    lines = []
    for s in run.steps:
        line = (f"- {s.step_id} [{s.status}] {s.intent}（action={s.action}"
                f"{f', 定位={s.used_strategy}' if s.used_strategy else ''}"
                f"{f', {s.duration_ms}ms' if s.duration_ms else ''}）")
        if s.category:
            line += f" ← 规则分类: {s.category}"
        if s.error:
            line += f"\n    错误: {s.error[:200]}"
        if s.warning:
            line += f"\n    备注: {s.warning[:120]}"
        lines.append(line)
    return "\n".join(lines)


def _failed_logcat(run: RunResult, artifacts_dir: Path, limit: int = 2000) -> str:
    chunks: list[str] = []
    for s in run.steps:
        if s.status == "passed" or not s.logcat_file:
            continue
        try:
            content = (artifacts_dir / s.logcat_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if content.strip():
            chunks.append(f"--- {s.step_id}（{s.logcat_file}）---\n{content[-limit:]}")
    return "\n".join(chunks) or "（无 logcat 证据）"


def _rule_prior(run: RunResult) -> str:
    return " · ".join(f"{_CATEGORY_LABELS.get(k, k)}×{v}"
                      for k, v in run.failed_categories.items()) or "无失败"


def build_variables(run: RunResult, artifacts_dir: Path) -> dict:
    return {
        "steps_summary": _steps_summary(run),
        "logcat": _failed_logcat(run, artifacts_dir),
        "rule_prior": _rule_prior(run),
    }


def _parse(content, run: RunResult) -> dict[str, dict] | None:
    """解析 LLM 输出 → {step_id: 归因}；非法输出返回 None（走规则回退）。

    ⚠️ **`content` 可能是 dict 也可能是 str，两种都要接住。**
    `complete_task_raw()` 的契约是「返回**解析后的 JSON**」（即 dict），
    而测试桩/历史调用可能直接给字符串。

    实测踩到（2026-10-03，T28 第一次真跑）：这里**只按 str 处理** ⇒
    `AttributeError: 'dict' object has no attribute 'strip'`；
    而崩点在 `diagnose_run` 的 try **外面** ⇒ **整个 run 崩**，不是降级成规则兜底。
    之所以一直没暴露：`diagnose` 此前**零测试**（立项目由）。
    """
    from .ai_client import GenClient

    if isinstance(content, dict):
        data = content
    else:
        try:
            data = json.loads(GenClient._strip_fences(content))
        except (ValueError, TypeError):
            return None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return None
    known_ids = {s.step_id for s in run.steps if s.status != "passed"}
    out: dict[str, dict] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("step_id", ""))
        category = str(item.get("category", ""))
        if step_id not in known_ids or category not in ATTR_LABELS:
            continue  # 幻觉步骤/未知分类一律丢弃
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = [str(e)[:200] for e in (item.get("evidence") or []) if str(e).strip()][:3]
        out[step_id] = {
            "category": category,
            "confidence": round(confidence, 2),
            "evidence": evidence,
            "suggestion": str(item.get("suggestion", ""))[:300],
            "need_human": bool(item.get("need_human")) or confidence < CONFIDENCE_FLOOR,
            "source": "llm",
        }
    return out or None


def _rule_card(step) -> dict:
    """规则兜底卡：**没有 LLM 也要有结论**，只是标成需要人工确认（错误成本不对称）。

    「错误成本不对称」= 把"定位失败"误判成"产品缺陷"会让人白查一天；
    反之只是多看一眼。所以规则兜底宁可**一律标 need_human**，不假装有把握。
    """
    return {"category": step.category or "unknown", "confidence": None, "evidence": [],
            "suggestion": "LLM 归因不可用，请人工确认",
            "need_human": True, "source": "rules"}


def diagnose_run(run: RunResult, artifacts_dir: Path, client=None) -> dict[str, dict]:
    """对 run 的失败步骤做 LLM 归因。返回 {step_id: 归因卡}；**任何失败 → 规则兜底卡**。

    client 缺省用 GenClient.from_env()（BYOK/server 双模式自动适配）。

    ⚠️ **没有 LLM 时也必须给出结论**（`source="rules"` + `need_human=True`），
    **不能抛异常**——调用方（CLI / console / 失败样本回流）都依赖"这里永远有返回"。

    实测踩到（2026-10-03）：`GenClient.from_env()` 在缺 `GEN_BASE_URL/GEN_API_KEY/GEN_MODEL`
    时抛 `GenError`，而**它没有被 try 包住**（只有后面的 `complete_task_raw` 被包了）
    ⇒ 没配 LLM 的机器上**归因直接崩**，文档承诺的"规则兜底"根本够不到。
    评测线要在无 LLM 环境下收失败样本时撞上了这个。
    """
    failed = [s for s in run.steps if s.status != "passed"]
    if not failed:
        return {}
    if client is None:
        from .ai_client import GenClient

        try:
            client = GenClient.from_env()
        except Exception:  # noqa: BLE001 — 缺 Key/未配置都算"没有 LLM"，一律走规则兜底；
            # 这里刻意不区分异常类型：**归因不可用不该让整个 run 崩掉**
            return {s.step_id: _rule_card(s) for s in failed}
    from .ai_client import complete_task_raw, task_mode

    if task_mode(client) != "server" and not client.api_key:
        return {s.step_id: _rule_card(s) for s in failed}  # 无 Key：规则兜底
    variables = build_variables(run, artifacts_dir)
    try:
        content = complete_task_raw(client, "diagnose", variables, context="失败归因")
    except Exception:
        return {s.step_id: _rule_card(s) for s in failed} # 调用失败：规则兜底（硬约束）
    parsed = _parse(content, run) or {}
    # 未被 LLM 覆盖的失败步骤补规则卡（错误成本不对称）
    for s in failed:
        parsed.setdefault(s.step_id, _rule_card(s))
    return parsed
