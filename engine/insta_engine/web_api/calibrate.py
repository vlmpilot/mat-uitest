"""报告校准域：/api/calibrate 数据 + /api/calibrate/apply。

与 CLI `mat calibrate` 同一套底层（calibration.py）：
- 数据：build_items（逐失败步骤：当前分类/证据/LLM 归因/断言期望-实际）；
- 应用：apply_calibration（改判写 run.json 标 human + 断言校准改用例出
  `<名>-calibrated.json` 不覆盖原用例 + 规则落盘让下次自动归类）+ 报告重渲染。

⚠️ apply 只限本机（`_local_only`）：规则会改变后续所有 run 的判定，不能暴露给局域网。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .. import calibration as cal_mod
from .runtime import _require_fresh_code
from .workspace import _local_only


def api_calibrate_data(root: Path, run: str) -> tuple[int, dict]:
    """GET /api/calibrate?run=：校准界面所需的一切（不含 logcat 全文——按需取）。

    收 passed 步骤（include_passed=True，发起人 2026-10-13）：成功用例也要能校准
    「是否误判」；前端把 passed 步骤折叠展示。
    """
    if not run or "/" in run or ".." in run:
        return 400, {"error": "非法执行目录"}
    run_dir = root / "artifacts" / run
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return 404, {"error": f"未找到 {run_json}"}
    try:
        payload = json.loads(run_json.read_text(encoding="utf-8"))
    except ValueError as exc:
        return 400, {"error": f"run.json 解析失败: {exc}"}
    items = cal_mod.build_items(payload, run_dir, include_passed=True)
    for it in items:
        if it.get("screenshot"):
            it["screenshot_url"] = f"/artifacts/{run}/{it['screenshot']}"
    return 200, {
        "run": run,
        "run_label": (payload.get("run") or {}).get("case_name") or run,
        "items": items,
        "categories": list(cal_mod.CATEGORIES),
        "category_labels": cal_mod.CATEGORY_LABELS,
        "attr_labels": cal_mod.ATTR_LABELS,
        "rules_count": len(cal_mod.load_rules(root)),
    }


def api_calibrate_apply(root: Path, host: str, body: dict) -> tuple[int, dict]:
    """POST /api/calibrate/apply：应用校准结论（改判+规则+断言校准），三层全落。

    body = {run: 目录名, steps: {step_id: {category?, error_contains?, logcat_contains?,
                                          note?, assertion_fix?, assertion_expected?,
                                          assertion_actual?}}}
    服务端**自己构造 run 标签**（防把结论应用到别的 run——与 CLI 的防错核对同源）。
    """
    gate = _local_only(host)
    if gate:
        return gate
    run = str(body.get("run", "")).strip()
    steps = body.get("steps")
    if not run or "/" in run or ".." in run:
        return 400, {"error": "非法执行目录"}
    if not isinstance(steps, dict) or not steps:
        return 400, {"error": "steps 必须是非空对象（step_id → 校准项）"}
    run_dir = root / "artifacts" / run
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return 404, {"error": f"未找到 {run_json}"}
    try:
        payload = json.loads(run_json.read_text(encoding="utf-8"))
    except ValueError as exc:
        return 400, {"error": f"run.json 解析失败: {exc}"}

    _require_fresh_code()  # 改 run.json/规则/用例都是写盘：跑在旧代码上等于白校准
    cal = {"run": run_dir.name, "steps": steps}
    stats = cal_mod.apply_calibration(payload, cal)
    render_warning = None
    if stats["applied"] or stats["marks"]:   # 纯标记类校准（可忽略/误判/note）同样要落盘
        run_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        try:
            from ..report import render_html
            from ..runner import RunResult
            render_html(RunResult.model_validate(payload["run"]), run_dir)
        except Exception as exc:  # noqa: BLE001 —— 报告重渲染失败不该让校准白做
            render_warning = f"报告重渲染失败（run.json 已改）: {exc}"

    if stats["rules"]:
        cal_mod.save_rules(root, stats["rules"], source="控制台校准（在线应用）")

    # 断言校准：改**用例**（`<名>-calibrated.json`，照 heal 先例不覆盖原用例）
    calibrated_out = None
    if stats["assertion_fixes"]:
        case_id = str((payload.get("run") or {}).get("case_id") or "")
        case_file = cal_mod.find_case_file(root, case_id)
        if case_file is not None:
            try:
                case = json.loads(case_file.read_text(encoding="utf-8"))
            except ValueError:
                case = None
            if case is not None:
                patched, notes = cal_mod.patch_case(case, stats["assertion_fixes"])
                out = case_file.with_name(f"{case_file.stem}-calibrated.json")
                out.write_text(json.dumps(patched, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                calibrated_out = {"file": str(out.relative_to(root)), "notes": notes}

    # 存档（结论与产物目录放一起，便于回溯）
    try:
        cal_mod.calibration_path(run_dir).write_text(
            json.dumps(cal, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass

    return 200, {"ok": True, "applied": len(stats["applied"]),
                 "unchanged": len(stats["unchanged"]),
                 "missing": stats["missing"],
                 "marked": len(stats["marks"]),
                 "rules_saved": len(stats["rules"]),
                 "rules_path": str(cal_mod.rules_path(root).relative_to(root)) if stats["rules"] else None,
                 "calibrated": calibrated_out,
                 "warning": render_warning}


def api_explore_calibrate(root: Path, host: str, body: dict) -> tuple[int, dict]:
    """POST /api/explore/calibrate：探索异常的人工校准（确认 / 忽略），回写 + 重渲染。

    body = {explore_id: 目录名, findings: {"<下标>": {verdict: "confirmed"|"ignored"|"",
                                                      note?: str}}}
    - verdict="confirmed"：确认是真异常；"ignored"：误报可忽略；""：清除标注（撤销）。
    - explore.json 原始 findings 不删不改（除追加 calibration 字段）——列表计数口径不变；
      重渲染 report.html 挂「已确认/已忽略」徽标。
    - 与 run 校准同一条纪律：`_local_only`（产物判定不能暴露给局域网）。
    """
    gate = _local_only(host)
    if gate:
        return gate
    exp_id = str(body.get("explore_id") or body.get("run") or "").strip()
    marks = body.get("findings")
    if not exp_id or "/" in exp_id or ".." in exp_id:
        return 400, {"error": "非法探索目录"}
    if not isinstance(marks, dict) or not marks:
        return 400, {"error": "findings 必须是非空对象（下标 → 校准项）"}
    exp_dir = root / "artifacts" / "explore" / exp_id
    exp_json = exp_dir / "explore.json"
    if not exp_json.exists():
        return 404, {"error": f"未找到 {exp_json}"}
    try:
        report = json.loads(exp_json.read_text(encoding="utf-8"))
    except ValueError as exc:
        return 400, {"error": f"explore.json 解析失败: {exc}"}
    findings = report.get("findings")
    if not isinstance(findings, list):
        return 400, {"error": "explore.json 缺少 findings 数组"}

    _require_fresh_code()  # 改 explore.json/重渲染都是写盘：跑在旧代码上等于白校准
    confirmed = ignored = cleared = 0
    changed = False
    missing: list[str] = []
    for idx_s, entry in marks.items():
        try:
            idx = int(idx_s)
        except (TypeError, ValueError):
            missing.append(str(idx_s))
            continue
        if not isinstance(entry, dict) or not (0 <= idx < len(findings)):
            missing.append(str(idx_s))
            continue
        verdict = str(entry.get("verdict") or "").strip()
        note = str(entry.get("note") or "").strip()
        if verdict in ("confirmed", "ignored"):
            findings[idx]["calibration"] = {
                "verdict": verdict, "note": note,
                "operator": str(body.get("operator") or ""),
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            changed = True
            if verdict == "confirmed":
                confirmed += 1
            else:
                ignored += 1
        elif verdict == "":
            if findings[idx].pop("calibration", None) is not None:
                cleared += 1
                changed = True
        else:
            missing.append(str(idx_s))

    warning = None
    if changed:
        exp_json.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        try:
            from ..report import render_explore_html
            render_explore_html(report, exp_dir)
        except Exception as exc:  # noqa: BLE001 —— 重渲染失败不该让校准白做
            warning = f"报告重渲染失败（explore.json 已改）: {exc}"
    return 200, {"ok": True, "confirmed": confirmed, "ignored": ignored,
                 "cleared": cleared, "missing": missing, "warning": warning}
