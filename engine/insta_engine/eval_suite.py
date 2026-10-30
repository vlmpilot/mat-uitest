"""评测套件常驻入口（`mat eval`）——把一次性跑批收敛成可重复、可守门的机制。

## 为什么需要它（2026-10-26）

改 prompt / 换模型 / 动引擎之后，**没有"跟基线比是否退化"的自动判定**——每次靠
手动跑批 + 人肉对比；对外数字（如 m1-thirdparty 中位 100% · 极差 2.1pp）的可复现
依赖"记得当时怎么跑的"。本模块把那次跑批（`scripts/spike_check.py`，D35 素材）
的**口径**收敛成常驻工具：口径来源 T34（数字绑口径）/ T76（靶场指纹）/ T60
（设备异常不炸全程）。

## 与 spike_check.py 的关系

spike_check 保留作**历史批次复现脚本**（10-25 跑批用它跑的，不动）；本模块是
正式实现。两者的 summarize 口径必须一致（中位/极差、通过·稳定失败·不稳三态、
风控排除出分母、env_error 单列）——`test_eval_suite.py` 锁定该口径，两处改动
须同步。

## 分层

- L0 CI 级：pytest（无设备）——已有。
- **L2 真机评测级（本模块）**：插设备手动触发，`mat eval run` + `mat eval compare`。
- 不做：评测平台 / Web 界面 / 定时自动跑批（单人项目，价值在口径可信，不在界面）。
"""

from __future__ import annotations

import json
import statistics
import subprocess  # noqa: F401 —— 保留给后续指纹增强用；re 同
from datetime import UTC, datetime
from pathlib import Path

from . import harmony_hdc as hdc
from .adb import adb
from .driver import app_apk_sha256, app_version
from .driver_factory import connect_driver
from .dsl import TestCase
from .report import render_html
from .risk import detect_in_xml
from .runner import Runner

#: 连续被风控拦住多少个任务后中止整场（风控不会自己消失，继续跑纯属浪费）
MAX_BLOCKED_STREAK = 3

#: baseline 对比的默认退化阈值（百分点）：中位通过率掉超过它 ⇒ FAIL
DEFAULT_REGRESSION_PP = 5.0

SCHEMA = "mat.eval/1"


def _risk_blocked(serial: str | None, platform: str = "android") -> str | None:
    """当前屏幕是否被风控拦住（口径同 spike_check：取不到屏幕按未命中，宁漏不误）。"""
    try:
        xml = connect_driver(serial, platform).dump()
    except Exception:  # noqa: BLE001 —— 不区分异常类型，保守按未命中
        return None
    return detect_in_xml(xml)


def summarize(results: list[dict], repeat: int, threshold: float) -> dict:
    """汇总口径（与 spike_check.summarize 同源，改动须双向同步）。

    - per_run：逐遍（tasks/steps/passed/rate/blocked/env_error）
    - per_task：逐任务（passed/valid/blocked）；有性能样本时附 `perf` 中位
      （单任务趋势看这里，全局中位只在 overall）
    - 三态分开：N/N 通过、0/N **稳定失败**（不是 flaky——断言写错或产品真不满足）、
      0<p<N 不稳（真 flaky）
    - app_versions / app_fingerprints：靶场指纹（T76，数字自带"测的是哪个构建"）
    - crash：**真崩溃**（failed_categories 含 app-crash）的任务×遍计数与任务名——
      评测报告要能回答"这轮有没有真崩溃"，不能只让人翻 run.json
    """
    per_task: dict[str, dict] = {}
    for r in results:
        d = per_task.setdefault(r["task"], {"id": r["task"], "passed": 0, "valid": 0,
                                            "blocked": 0})
        if r.get("blocked"):
            d["blocked"] += 1
            continue
        d["valid"] += 1
        d["passed"] += 1 if r["status"] == "passed" else 0

    # per_task perf：只有该任务有采集样本时才挂 perf 段（缺值不进统计，与 overall 同口径）
    perf_by_task: dict[str, dict[str, list[float]]] = {}
    for r in results:
        perf = r.get("perf") or {}
        bucket = perf_by_task.setdefault(r["task"], {"launch": [], "mem": []})
        if perf.get("launch_ms") is not None:
            bucket["launch"].append(perf["launch_ms"])
        if perf.get("mem_pss_mb") is not None:
            bucket["mem"].append(perf["mem_pss_mb"])
    for tid, bucket in perf_by_task.items():
        if not bucket["launch"] and not bucket["mem"]:
            continue
        per_task[tid]["perf"] = {
            "launch_ms_median": round(statistics.median(bucket["launch"]))
            if bucket["launch"] else None,
            "mem_pss_mb_median": round(statistics.median(bucket["mem"]), 1)
            if bucket["mem"] else None,
            "samples": max(len(bucket["launch"]), len(bucket["mem"])),
        }

    per_run: list[dict] = []
    for run_idx in range(1, repeat + 1):
        rs = [r for r in results if r["run"] == run_idx and not r.get("blocked")
              and not r.get("env_error")]
        steps = sum(r["total"] for r in rs)
        ok = sum(r["passed"] for r in rs)
        per_run.append({"run": run_idx, "tasks": len(rs), "steps": steps, "passed": ok,
                        "rate": round(ok / steps, 4) if steps else 0.0,
                        "blocked": len([r for r in results if r["run"] == run_idx
                                        and r.get("blocked")]),
                        "env_error": len([r for r in results if r["run"] == run_idx
                                          and r.get("env_error")])})
    rates = [p["rate"] for p in per_run]
    overall = {"median": round(statistics.median(rates), 4) if rates else 0.0,
               "min": min(rates) if rates else 0.0,
               "max": max(rates) if rates else 0.0,
               "spread_pp": round((max(rates) - min(rates)) * 100, 1) if rates else 0.0}
    # G7 自愈口径：触发兜底的步骤中，该步最终转过的比例（分母=触发数；无触发 ⇒ None）
    heal_triggered = sum(r.get("heal_triggered", 0) for r in results)
    heal_recovered = sum(r.get("heal_recovered", 0) for r in results)
    overall["heal"] = {"triggered": heal_triggered, "recovered": heal_recovered,
                       "rate": round(heal_recovered / heal_triggered, 4)
                       if heal_triggered else None}
    # 性能聚合（best-effort 采集，缺值不进统计）：任务级中位
    launches = [r["perf"]["launch_ms"] for r in results
                if (r.get("perf") or {}).get("launch_ms") is not None]
    mems = [r["perf"]["mem_pss_mb"] for r in results
            if (r.get("perf") or {}).get("mem_pss_mb") is not None]
    overall["perf"] = {"launch_ms_median": round(statistics.median(launches)) if launches else None,
                       "mem_pss_mb_median": round(statistics.median(mems), 1) if mems else None}
    # 真崩溃（app-crash）按「任务×遍」计数：评测报告必须能回答"这轮有没有真崩溃"。
    # 同一任务多遍都崩 -> 多条信号（不是去重后的任务数）；companion 字段 tasks 给任务名。
    crash_results = [r for r in results
                     if (r.get("categories") or {}).get("app-crash")]
    overall["crash"] = {
        "task_runs": len(crash_results),
        "tasks": sorted({r["task"] for r in crash_results}),
        "runs": [f"{r['task']}#r{r['run']}" for r in crash_results],
    }

    stable_fail = [d["id"] for d in per_task.values() if d["valid"] and d["passed"] == 0]
    unstable = [d["id"] for d in per_task.values()
                if d["valid"] and 0 < d["passed"] < d["valid"]]
    return {"per_task": list(per_task.values()), "per_run": per_run, "overall": overall,
            "unstable": unstable, "stable_fail": stable_fail,
            "blocked_total": len([r for r in results if r.get("blocked")]),
            "app_versions": sorted({r.get("app_version") or "" for r in results} - {""}),
            "app_fingerprints": sorted({r.get("app_apk_sha256") or "" for r in results} - {""}),
            "verdict": "PASS" if overall["median"] >= threshold else "FAIL"}


def _run_task(task: dict, serial: str | None, suite: dict, run_idx: int, repeat: int,
              artifacts_root: Path, quality_gate: bool,
              platform: str = "android") -> dict:
    """跑一次任务（口径同 spike_check._run_task：按任务覆盖清包、设备异常就地接住、
    多遍按 r<N> 分子目录防报告覆盖、记录靶场指纹）。

    platform="harmony" 时驱动走 factory(harmony)；清包(bm clean)/指纹(appIdentifier)
    /风控检测均按平台分派。perf 采集(Android 专属命令)best-effort,鸿蒙上失败即 None。"""
    case = TestCase.model_validate(task["case"])
    pkg = (case.target or {}).get("package")
    reset_pkg = task.get("_reset_package") or suite.get("reset_package")
    if reset_pkg and pkg == reset_pkg:
        if platform == "android":
            adb(serial, "shell", "pm", "clear", pkg)
        elif platform == "harmony":
            # bm clean -d(数据目录)——真机校准 2026-10-27;失败不炸跑批(冷启动尽力)
            try:
                hdc.shell(f"bm clean -n {pkg} -d", serial=serial, timeout=30)
            except Exception:  # noqa: BLE001 —— 清包失败按「没清」继续(与 adb 失败同口径)
                pass
    artifacts = artifacts_root / (task["id"] if repeat == 1
                                  else f"r{run_idx}/{task['id']}")
    try:
        drv = connect_driver(serial, platform)
        run = Runner(drv, artifacts, quality_gate=quality_gate).run(case)
    except Exception as exc:  # noqa: BLE001 —— 驱动异常统一按环境问题处置（T60）
        return {"task": task["id"], "run": run_idx, "status": "device-error",
                "passed": 0, "total": len(case.steps), "blocked": None,
                "env_error": f"{type(exc).__name__}: {exc}"}
    render_html(run, artifacts)
    ok = sum(s.status == "passed" for s in run.steps)
    risk = _risk_blocked(serial, platform)
    # G7 自愈口径：触发兜底（step.heal 有记录）后**该步最终转过**的比例——
    # 兜底成功但动作仍失败 = 未恢复，不算自愈成功。perf 随评测天然采集（best-effort）。
    # 靶场指纹（T76）：Android 走 getprop/apk 摘要;鸿蒙用 appIdentifier 哈希
    # (包名只够分辨「哪个 App」，分辨不出版本号);取不到空串不静默冒充
    if platform == "android":
        try:
            version, fp = app_version(drv, pkg or ""), app_apk_sha256(drv, pkg or "")
        except Exception:  # noqa: BLE001
            version, fp = "", ""
    elif platform == "harmony" and pkg and hasattr(drv, "app_fingerprint"):
        try:
            version, fp = "", drv.app_fingerprint(pkg)
        except Exception:  # noqa: BLE001
            version, fp = "", ""
    else:
        version, fp = "", ""
    return {"task": task["id"], "run": run_idx, "passed": ok, "total": len(run.steps),
            "status": run.status, "blocked": risk, "artifacts": str(artifacts),
            "app_version": version,
            "app_apk_sha256": fp,
            "perf": {"launch_ms": run.perf.get("launch_ms"),
                     "mem_pss_mb": run.perf.get("mem_pss_mb")},
            "categories": dict(run.failed_categories),
            "heal_triggered": sum(1 for s in run.steps if s.heal),
            "heal_recovered": sum(1 for s in run.steps if s.heal and s.status == "passed")}


def run_eval(suite_path: Path, *, repeat: int = 1, serial: str | None = None,
             artifacts_root: Path | None = None, quality_gate: bool = False,
             threshold: float | None = None, on_progress=None,
             platform: str = "android") -> dict:
    """执行评测套件并产出 report（schema mat.eval/1）。设备异常**中止后续遍**但
    照常汇总已完成部分（T60）；连续风控拦截 MAX_BLOCKED_STREAK 个也中止。"""
    suite_path = Path(suite_path)
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    tasks = suite["tasks"]
    repeat = max(1, repeat)
    threshold = float(threshold if threshold is not None
                      else suite.get("pass_threshold", 0.8))
    artifacts_root = artifacts_root or Path("artifacts") / "eval" / suite_path.stem
    report_dir = Path("artifacts") / "eval"
    report_dir.mkdir(parents=True, exist_ok=True)

    # 路由口径清零：报告里的 `route_mix` 必须是**这一批**的，不能带上历史累计
    from . import ai_client

    ai_client.route_reset()
    results: list[dict] = []
    blocked_streak = 0
    aborted = False
    for run_idx in range(1, repeat + 1):
        if aborted:
            break
        for task in tasks:
            r = _run_task(task, serial, suite, run_idx, repeat, artifacts_root,
                          quality_gate, platform)
            results.append(r)
            if on_progress:
                on_progress(r)
            if r.get("env_error"):
                aborted = True  # 设备没了 ⇒ 继续只会全错；已完成部分照常汇总（T60）
                break
            if r.get("blocked"):
                blocked_streak += 1
                if blocked_streak >= MAX_BLOCKED_STREAK:
                    aborted = True
                    break
                continue
            blocked_streak = 0

    sm = summarize(results, repeat, threshold)
    report = {
        "schema": SCHEMA,
        "suite": suite_path.name,
        "repeat": repeat,
        "threshold": threshold,
        "aborted": aborted,
        "quality_gate": quality_gate,
        "serial": serial,
        "platform": platform,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_steps_planned": sum(len(t["case"]["steps"]) for t in tasks) * repeat,
        # 本批实际走了哪条上游（local / maas / unknown）：回落是静默的，
        # 没有这个字段就无法判断"两批数字能不能直接比"（部署文档里记了判读方法）
        "route_mix": ai_client.route_snapshot(),
        **sm,
    }
    out = report_dir / f"{suite_path.stem}-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    report["report_path"] = str(out)
    return report


def compare(report: dict, baseline: dict,
            threshold_pp: float = DEFAULT_REGRESSION_PP) -> dict:
    """与基线对比，产出回归判定（发版守门：FAIL ⇒ exit 1）。

    判定项：① 中位通过率退化 > threshold_pp；② 新增**稳定失败**任务（原来通过
    现在必挂——最危险的回归）；③ 新增**不稳**任务。improvements 反向列出，
    供发版说明引用。**指纹变化本身不判回归**（App 升级是正常输入），但会在
    note 里提示口径差异。
    """
    base_tasks = {d["id"]: d for d in baseline.get("per_task", [])}
    cur_tasks = {d["id"]: d for d in report.get("per_task", [])}
    regressions: list[str] = []
    improvements: list[str] = []

    dm = round((baseline["overall"]["median"] - report["overall"]["median"]) * 100, 1)
    if dm > threshold_pp:
        regressions.append(
            f"中位通过率退化 {dm}pp（{baseline['overall']['median']} → "
            f"{report['overall']['median']}，阈值 {threshold_pp}pp）")
    elif dm < -threshold_pp:
        improvements.append(f"中位通过率提升 {-dm}pp")

    for tid, d in base_tasks.items():
        c = cur_tasks.get(tid)
        if not c:
            continue
        was_pass = bool(d["valid"]) and d["passed"] == d["valid"]
        now_fail = bool(c["valid"]) and c["passed"] == 0
        if was_pass and now_fail:
            regressions.append(f"{tid}: 通过 → 稳定失败")
        was_flaky = bool(d["valid"]) and 0 < d["passed"] < d["valid"]
        now_flaky = bool(c["valid"]) and 0 < c["passed"] < c["valid"]
        if not was_flaky and now_flaky:
            regressions.append(f"{tid}: 新增不稳（{c['passed']}/{c['valid']}）")
        was_fail = bool(d["valid"]) and d["passed"] == 0
        now_pass = bool(c["valid"]) and c["passed"] == c["valid"]
        if was_fail and now_pass:
            improvements.append(f"{tid}: 稳定失败 → 通过")

    # 指纹对比用**子集语义**：本次 ⊆ 基线（如只跑了基线中部分 App 的演练套件）是
    # 正常输入，不算口径差异；出现基线没有的**新指纹**才提示（跨了新构建）
    base_fps = set(baseline.get("app_fingerprints") or [])
    cur_fps = set(report.get("app_fingerprints") or [])
    fp_changed = bool(cur_fps - base_fps)

    # 性能守门（perf.py 采集，eval 天然携带）：中位数劣化 >30% 标红。
    # 指纹变化（App 升级）时跳过——不同构建的性能本就不可比。
    base_perf = (baseline.get("overall") or {}).get("perf") or {}
    cur_perf = (report.get("overall") or {}).get("perf") or {}
    perf_skipped = bool(fp_changed)
    for key, label in (("launch_ms_median", "启动耗时"), ("mem_pss_mb_median", "内存 PSS")):
        b, c = base_perf.get(key), cur_perf.get(key)
        if perf_skipped or not b or not c:
            continue
        if c / b > 1.3:
            regressions.append(f"{label} 劣化 {round((c / b - 1) * 100)}%（{b} → {c}）")

    # 新增崩溃任务：真产品缺陷信号，但不自动判 FAIL（App 升级常引入行为变化）——
    # 如实放进 note，让人确认。
    base_crash = set((baseline.get("overall") or {}).get("crash", {}).get("tasks") or [])
    cur_crash = set((report.get("overall") or {}).get("crash", {}).get("tasks") or [])
    new_crash = sorted(cur_crash - base_crash)

    verdict = "FAIL" if regressions else "PASS"
    notes = []
    if fp_changed:
        notes.append("靶场指纹变化——数字跨的是不同 App 构建，引用时须注明")
    if new_crash:
        notes.append(f"新增崩溃任务 {len(new_crash)} 个（{'、'.join(new_crash[:5])}"
                     f"{'…' if len(new_crash) > 5 else ''}）——可能是 App 变化引入的真缺陷，人工确认")

    # 路由口径（2026-10-30）：基线走本地、这次走官方（或反之）时，中位数差异里混着
    # "生成模型不同"这一层——不能当回归/改善引用。只进 note，不判 FAIL（那不是产品回归）。
    def _dominant(mix: dict | None) -> str:
        m = {k: int(v or 0) for k, v in (mix or {}).items()}
        return max(m, key=lambda k: m[k]) if sum(m.values()) else ""

    base_route, cur_route = _dominant(baseline.get("route_mix")), _dominant(report.get("route_mix"))
    if base_route and cur_route and base_route != cur_route:
        notes.append(f"路由不同（基线 {base_route} / 本次 {cur_route}）——"
                     "生成模型不同，中位差异不能当回归或改善引用")
    return {"schema": SCHEMA, "compare": True,
            "verdict": verdict, "regressions": regressions,
            "improvements": improvements,
            "median_delta_pp": -dm,
            "app_fingerprints_changed": fp_changed,
            "note": "；".join(notes)}


def save_baseline(report: dict, suite_stem: str, root: Path | None = None) -> Path:
    """把 report 存为该套件的基线（`mat eval run --save-baseline`）。"""
    root = root or Path("artifacts") / "eval"
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"baseline-{suite_stem}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                 encoding="utf-8")
    return p


def summary_line(report: dict) -> str:
    """对外可引用一行（T34 口径：数字与限制绑在一起，杜绝手抄丢口径）。

    ⚠️ **精度跟极差走**：spread 0 用整数百分比、否则一位小数——实测踩过
    「330/330 与 329/330 都四舍五入成 100%，但极差 0.3pp」的自相矛盾。不稳/风控/
    不完整是**引用限制**，必须跟数字同句出现。
    """
    o = report["overall"]
    fmt = "{:.1%}" if o["spread_pp"] > 0 else "{:.0%}"
    parts = [f"步骤成功率中位 {fmt.format(o['median'])}，极差 {fmt.format(o['min'])}–"
             f"{fmt.format(o['max'])}（{o['spread_pp']:.1f}pp）",
             f"{len(report.get('per_task', []))} 条任务 × {report.get('repeat')} 遍"]
    fps = report.get("app_fingerprints") or []
    if fps:
        parts.append(f"指纹 {'、'.join(fps[:2])}{'…' if len(fps) > 2 else ''}")
    if report.get("aborted"):
        parts.append("⚠️ 本场不完整（中止）")
    if report.get("blocked_total"):
        parts.append(f"{report['blocked_total']} 个「任务×遍」被风控拦截（已排除出分母）")
    unstable = report.get("unstable") or []
    if unstable:
        parts.append(f"不稳 {len(unstable)} 条（{'、'.join(unstable[:3])}"
                     f"{'…' if len(unstable) > 3 else ''}）⇒ 单遍结果不可引用")
    crash = (report.get("overall") or {}).get("crash") or {}
    if crash.get("task_runs"):
        parts.append(f"崩溃 {crash['task_runs']} 个「任务×遍」"
                     f"（{'、'.join((crash.get('tasks') or [])[:4])}）")
    mix = {k: int(v or 0) for k, v in (report.get("route_mix") or {}).items()}
    if sum(mix.values()):
        local, maas, unknown = mix.get("local", 0), mix.get("maas", 0), mix.get("unknown", 0)
        if local == 0 and maas:
            parts.append(f"本批全部走官方模型（{maas} 次调用，本地未命中）"
                         "——数字不可与走本地的一批直接比")
        elif local and maas:
            parts.append(f"路由混源：本地 {local} / 官方 {maas} ⇒ 数字含两种模型")
        elif unknown:
            parts.append(f"路由未上报 {unknown} 次（服务端版本较旧）")
    return " · ".join(parts)
