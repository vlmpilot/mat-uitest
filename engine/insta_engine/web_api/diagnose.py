"""diagnose 域：/api/diagnose（对一次执行的失败步骤做 LLM 归因，job 化）。"""

from __future__ import annotations

import json
from pathlib import Path

from .runtime import _require_fresh_code, operator_of, start_job


def api_diagnose(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/diagnose：与 CLI `mat diagnose artifacts/<产物目录>` 同语义。

    - LLM 归因写 `diagnose.json` 并重渲染报告（归因卡挂上）；
    - 无 GEN 配置 ⇒ job 内报可读错误（配置指引）；
    - 无失败步骤 ⇒ 400 直接说明。
    """
    run = str(body.get("run", "")).strip()
    if not run or "/" in run or ".." in run:
        return 400, {"error": "非法执行目录"}
    run_dir = root / "artifacts" / run
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return 404, {"error": f"未找到 {run_json}（用法：对 artifacts/<产物目录> 归因）"}
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
        from ..runner import RunResult
        run_obj = RunResult.model_validate(data["run"])
    except (ValueError, KeyError) as exc:
        return 400, {"error": f"run.json 解析失败: {exc}"}
    # 用例对象（可选）：给归因注入页面图谱证据用；旧产物没有就静默降级。
    case_obj = None
    if isinstance(data.get("case"), dict):
        try:
            from ..dsl import TestCase
            case_obj = TestCase.model_validate(data["case"])
        except Exception:  # noqa: BLE001 — 证据是加分项，解析失败不拦归因
            pass
    failed = [s for s in run_obj.steps if s.status != "passed"]
    if not failed:
        return 400, {"error": "本次执行无失败步骤，无需归因"}

    def job(emit, cancel):
        from ..ai_client import GenClient
        from ..diagnose import diagnose_run
        from ..report import render_html

        _require_fresh_code()
        client = GenClient.from_env()
        mode = "server" if getattr(client, "prompt_mode", "local") == "server" else (
            "BYOK" if client.api_key else "无 Key")
        emit("diagnose", "running", f"归因 {len(failed)} 个失败步骤（模式: {mode}）")
        attributions = diagnose_run(run_obj, run_dir, client, case=case_obj,
                                    page_index_dir=root / "artifacts" / "page_index")
        (run_dir / "diagnose.json").write_text(
            json.dumps(attributions, ensure_ascii=False, indent=2), encoding="utf-8")
        render_html(run_obj, run_dir)  # 重渲染报告，挂上归因卡（含顶部汇总区 AI 归因列）
        return f"归因完成:{len(failed)} 个失败步骤（模式: {mode}）", \
            f"/artifacts/{run}/report.html#attr"  # #attr 直达顶部失败汇总的归因卡

    return 200, {"job_id": start_job("diagnose", job, meta={"operator": operator_of(body)})}
