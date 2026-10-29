"""首跑通过率统计——**从产物目录直接统计，不依赖终端输出**。

此前是 scripts/eval_first_run.py（一次性脚本）；2026-10-26 收敛为包内模块（单一实现）：
- `mat eval first-run <run_dirs...>` 是正式入口（产物 JSON 可被 `mat metrics` 消费）；
- scripts/eval_first_run.py 保留为薄壳（CLI 兼容）。

## 口径纪律

- 首跑 = 用例**未经人工修改**；如果跑之前改过用例，这个数就不是首跑通过率，是"改后通过率"；
- 必须带上包名、设备、日期一起报；
- k/n 口径：同一用例 ×N 次聚合为 k/n——单次结果不可引用。

## 指标

- **首跑通过率**（按包名分组 + 总体；剔除假红后一并报——单报一个都是误导）
- **失败分层**：环境 / 定位 / 断言 / 其他 —— 决定"该修哪一层"
- **C2 环境中断率**、**F1 证据完整率**（失败步截图+日志+分类三件齐全）、**C3 每用例耗时**中位数
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

#: 环境类失败的特征（这些不该算"用例写错了"）
_ENV_RE = re.compile(r"锁屏|Connection refused|connection|device offline|device not found|"
                     r"超时|timeout|adb.*error|usbmux|No such device", re.IGNORECASE)
#: 定位类失败
_LOCATE_RE = re.compile(r"LocateFailed|定位失败|未找到|not found in tree", re.IGNORECASE)
#: 断言类失败（用例写对了但 App 行为不符，或断言本身写错）
_ASSERT_RE = re.compile(r"AssertionError|断言失败", re.IGNORECASE)


def classify(error: str) -> str:
    text = error or ""
    if _ENV_RE.search(text):
        return "环境"
    if _LOCATE_RE.search(text):
        return "定位"
    if _ASSERT_RE.search(text):
        return "断言"
    return "其他"


def _first_error(run: dict) -> str:
    """**第一个真正的失败步**的报错。

    ⚠️ 必须跳过 `skipped`：R22 把「点跳过广告」这类步骤改成 `onFailure=continue` 之后，
    它们会带 `LocateFailed: text=跳过 未命中` 进 `error` 字段——拿它当"首条错误"，
    会把一条**断言失败**的用例读成"定位失败"（实测 2026-10-22 的 r8 失败清单全是这句）。
    """
    skipped_error = ""
    for step in run.get("steps") or []:
        if step.get("status") == "passed" or not step.get("error"):
            continue
        if step.get("status") == "skipped":
            skipped_error = skipped_error or str(step["error"])
            continue
        return str(step["error"])
    return skipped_error


def _first_failed_step(run: dict) -> dict | None:
    """第一个**真正的失败步**（跳过 `skipped`，同 `_first_error` 的口径）。

    假红标记挂在失败步上（`StepResult.false_red`），所以要先拿到那一步，
    而不是只看错误文本——文本层面 `RemoteDisconnected` 与真断连长得一样。
    """
    for step in run.get("steps") or []:
        if step.get("status") in ("passed", "skipped"):
            continue
        return step
    return None


def _is_false_red(run: dict) -> bool:
    """本次运行是否被判为**传输层假红**（P0-1 标记，见 runner 的 env 复核）。

    语义：分类为 `env-failure` 但 App 当时稳在前台 ⇒ 设备/用例都没问题，
    是 u2 会话断了。这类**不是**用例质量问题，也不是真的环境不可用。
    """
    step = _first_failed_step(run)
    return bool(step and step.get("false_red"))


def _evidence_ok(run: dict, case_dir: Path) -> bool:
    """失败步是否三件齐全：截图文件存在 + 日志文件存在 + 有分类。"""
    for step in run.get("steps") or []:
        if step.get("status") == "passed":
            continue
        shot = step.get("screenshot")
        log = step.get("logcat_file")
        has_shot = bool(shot) and (case_dir / str(shot)).exists()
        has_log = bool(log) and (case_dir / str(log)).exists()
        return bool(has_shot and has_log and step.get("category"))
    return False


def summarize(dirs: list[Path]) -> dict:
    rows: list[dict] = []
    for base in dirs:
        # ⚠️ 用 rglob 而不是 glob("*/run.json")：多轮跑批的产物是
        # `artifacts/<批次>/r<N>/<task>/run.json`（`spike_check --repeat`），
        # 一层 glob 会显示「没有产物」——与 coverage/classify 同族。
        for path in sorted(base.rglob("run.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            run = doc.get("run") or {}
            case_dir = path.parent
            failed = run.get("status") != "passed"
            rows.append({
                "package": (doc.get("case") or {}).get("target", {}).get("package")
                or doc.get("app") or "?",
                "case": run.get("case_name") or run.get("case_id"),
                "status": run.get("status"),
                "duration_ms": run.get("duration_ms") or 0,
                "kind": classify(_first_error(run)) if failed else "",
                "false_red": _is_false_red(run) if failed else False,
                "evidence_ok": _evidence_ok(run, case_dir) if failed else None,
                "error": _first_error(run)[:160],
            })
    if not rows:
        return {"total": 0, "rows": []}

    by_pkg: dict[str, dict] = {}
    for row in rows:
        g = by_pkg.setdefault(row["package"], {"total": 0, "passed": 0, "env": 0,
                                              "false_red": 0, "kinds": {}})
        g["total"] += 1
        if row["status"] == "passed":
            g["passed"] += 1
        else:
            g["kinds"][row["kind"]] = g["kinds"].get(row["kind"], 0) + 1
            if row["kind"] == "环境":
                g["env"] += 1
            if row["false_red"]:
                g["false_red"] += 1

    # k/n 口径：同一用例 ×N 次聚合为 k/n——单次结果不可引用
    # （spike_check 自己的纪律），且「7 次同用例算 7 条」会把 n 灌水。按 (package, case)
    # 聚合：k = 通过次数，n = 跑了几次；k_ratio=k/n。
    per_case: dict[tuple, dict] = {}
    for row in rows:
        key = (row["package"], str(row["case"]))
        c = per_case.setdefault(key, {"id": row["case"], "package": row["package"],
                                      "n": 0, "passed": 0})
        c["n"] += 1
        if row["status"] == "passed":
            c["passed"] += 1
    for c in per_case.values():
        c["k_ratio"] = round(c["passed"] / c["n"], 3)
    # 环境中断按「每用例几次」报（一条用例跑 3 次坏 2 次 ⇒ env_runs=2），不按条数
    for row in rows:
        if row["kind"] == "环境":
            per_case[(row["package"], str(row["case"]))]["env_runs"] = \
                per_case[(row["package"], str(row["case"]))].get("env_runs", 0) + 1
    # 假红同样按「每用例几次」——它是**测量噪声**，混进 k/n 会把一条好用例读成不稳
    for row in rows:
        if row["false_red"]:
            per_case[(row["package"], str(row["case"]))]["false_red_runs"] = \
                per_case[(row["package"], str(row["case"]))].get("false_red_runs", 0) + 1

    failed_rows = [r for r in rows if r["status"] != "passed"]
    with_evidence = [r for r in failed_rows if r["evidence_ok"]]
    durations = [r["duration_ms"] for r in rows if r["duration_ms"]]
    passed_n = sum(1 for r in rows if r["status"] == "passed")
    false_red_n = sum(1 for r in rows if r["false_red"])
    return {
        "total": len(rows),
        "passed": passed_n,
        "by_package": by_pkg,
        "env_interrupted": sum(1 for r in rows if r["kind"] == "环境"),
        #: P0-1 假红：App 在前台却判成环境失败（传输层断连）⇒ **测量噪声**，
        #: 单独报，并给出「剔除假红后的通过率」——否则一次会话抖动就把整批数字压低。
        "false_red": false_red_n,
        "d3_ex_false_red": (round(passed_n / (len(rows) - false_red_n), 3)
                            if len(rows) > false_red_n else None),
        "evidence_rate": (len(with_evidence) / len(failed_rows)) if failed_rows else None,
        "duration_median_ms": int(statistics.median(durations)) if durations else None,
        "per_case": sorted(per_case.values(), key=lambda c: (c["package"], str(c["id"]))),
        "rows": rows,
    }


def render(summary: dict) -> str:
    if not summary["total"]:
        return "没有产物（目录里没有 */run.json）"
    total, passed = summary["total"], summary["passed"]
    false_red = summary.get("false_red") or 0
    lines = [f"首跑通过率: **{passed}/{total} = {passed / total:.0%}**"]
    if false_red:
        # 原始数字被测量噪声压低时，两个数一起报——单报一个都是误导
        ex = summary.get("d3_ex_false_red")
        lines.append(f"　剔除假红后: **{passed}/{total - false_red} = {ex:.0%}**"
                     f"（假红 {false_red} 次为传输层断连，非用例/环境问题）")
    lines.append("")
    lines.append(f"{'包名':28s} {'用例':>4s} {'通过':>4s} {'通过率':>6s}  失败分层")
    for pkg, g in sorted(summary["by_package"].items()):
        kinds = "、".join(f"{k}{v}" for k, v in sorted(g["kinds"].items())) or "-"
        lines.append(f"{pkg:28s} {g['total']:>4d} {g['passed']:>4d} "
                     f"{g['passed'] / g['total']:>5.0%}  {kinds}")
    lines.append("")
    if summary["env_interrupted"]:
        lines.append(f"⚠️ 环境中断 {summary['env_interrupted']} 条"
                     f"（占 {summary['env_interrupted'] / total:.0%}）——这些不该算用例质量问题")
    if false_red:
        lines.append(f"⚠️ 其中假红 {false_red} 条：分类为环境失败但 **App 当时在前台**"
                     "（传输层断连误诊）⇒ 既不是用例问题，也不是环境不可用；"
                     "单列，不进通过率分母（见上一行剔除口径）")
    if summary["evidence_rate"] is not None:
        lines.append(f"失败步证据完整率（F1）: {summary['evidence_rate']:.0%}")
    if summary["duration_median_ms"]:
        lines.append(f"每用例耗时中位（C3）: {summary['duration_median_ms'] / 1000:.1f}s")
    lines.append("")
    pc = summary.get("per_case") or []
    if pc:
        stable = [c for c in pc if c["n"] > 1]
        lines.append(f"按用例 k/n（P0-2 口径，同用例多次聚合；{len(stable)} 条 ×N):")
        for c in pc:
            env_note = f"（环境坏 {c['env_runs']} 次）" if c.get("env_runs") else ""
            fr = c.get("false_red_runs") or 0
            if fr:
                # 假红是噪声：报「剔除后 k/(n-假红)」，否则一条好用例会被读成不稳
                env_note += f"（假红 {fr} 次 ⇒ 有效 k/{c['n'] - fr}）"
            lines.append(f"  {str(c['id'])[:34]:36s} k/n = {c['passed']}/{c['n']}"
                         f" = {c['k_ratio']:.0%}{env_note}")
        lines.append("")
    lines.append("失败清单:")
    for row in summary["rows"]:
        if row["status"] == "passed":
            continue
        tag = "假红" if row.get("false_red") else row["kind"]
        lines.append(f"  [{tag}] {str(row['case'])[:34]:36s} {row['error'][:70]}")
    return "\n".join(lines)
