"""自愈提案域：/api/heals（列出）与 /api/heals/apply（生成修复用例，不覆盖原用例）。"""

from __future__ import annotations

import json
from pathlib import Path

from ..dsl import TestCase, dump_dsl
from ..heals import apply_heals, collect_proposals


def api_heal_list(root: Path) -> tuple[int, dict]:
    """GET /api/heals：扫描 artifacts/*/run.json，收集带自愈提案的执行。

    只对 `cases/<目录名>.json` 存在的执行给出 case 文件（批量套件目录没有单一
    原用例，apply 语义不成立 ⇒ 不列）。
    """
    runs: list[dict] = []
    artifacts = root / "artifacts"
    if artifacts.exists():
        for run_json in sorted(artifacts.glob("*/run.json"), reverse=True):
            run_dir = run_json.parent
            case_file = root / "cases" / f"{run_dir.name}.json"
            if not case_file.exists():
                continue
            try:
                data = json.loads(run_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            proposals = collect_proposals(run_dir)
            if not proposals:
                continue
            runs.append({
                "run": run_dir.name,
                "case": case_file.name,
                "case_name": (data.get("run") or {}).get("case_name"),
                "status": (data.get("run") or {}).get("status"),
                "proposals": proposals,
            })
    return 200, {"runs": runs}


def api_heal_apply(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/heals/apply：把该次执行的提案应用为 `cases/<名>-healed.json`。

    与 CLI `mat heal apply` 同语义：**不覆盖原用例**——修复版是独立文件，人工比对
    后再决定替换。提案里的 original 在当前用例中找不到时（用例已被改过）跳过并说明。
    """
    run = str(body.get("run", "")).strip()
    if not run or "/" in run or ".." in run:
        return 400, {"error": "非法执行目录"}
    run_dir = root / "artifacts" / run
    case_file = root / "cases" / f"{run}.json"
    if not run_dir.exists() or not case_file.exists():
        return 404, {"error": f"执行或原用例不存在: {run}"}
    try:
        proposals = collect_proposals(run_dir)
    except ValueError as exc:  # run.json 缺失/损坏：400 可行动报错，不是 500
        return 400, {"error": str(exc)}
    if not proposals:
        return 400, {"error": "该执行没有自愈提案"}
    try:
        case = TestCase.model_validate_json(case_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        return 400, {"error": f"原用例解析失败: {exc}"}
    new_case, notes = apply_heals(case, proposals)
    out = root / "cases" / f"{run}-healed.json"
    out.write_text(dump_dsl(new_case), encoding="utf-8")
    return 200, {"ok": True, "out": out.name, "notes": notes}
