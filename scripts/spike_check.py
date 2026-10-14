"""验收跑分：运行任务集的 gold DSL，统计步骤成功率。

用法：
  python scripts/spike_check.py [--suite benchmarks/tasks/m1-flows.json] [--serial <serial>]
                                [--artifacts artifacts/spike] [--threshold 0.8]
                                [--repeat 3] [--out artifacts/m1-summary.json]

缺省套件是 spike-5-tasks.json（保持原行为不变）；评测扩容用 `--suite` 指到自己的套件。

**为什么判定必须由这个脚本给出**：
单人标注做不了 kappa，替代方案是把主观判断挤出流程——**通过/失败由脚本判**，
不依赖"人看一眼觉得对不对"。所以评测集的每条任务都必须能在这里跑出通过/失败。

**为什么必须支持 `--repeat N`**：
实测同一批任务跑三次得 **89% / 96% / 92%**，而且**失败的任务集合每次不同**
⇒ **单次结果不可引用**。定的口径本来就是"每条跑 N 遍报极差"，
所以本脚本给出的是**逐任务通过分布 + 整体的中位与极差**，而不是一个孤零零的百分比。

⚠️ **对外引用时只引"中位 + 极差 + 样本量"，不要引单次数字**——脚本会把这句提醒印在结论里。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from insta_engine.adb import adb
from insta_engine.driver import DeviceDriver, app_apk_sha256, app_version
from insta_engine.dsl import TestCase
from insta_engine.report import render_html
from insta_engine.risk import detect_in_xml
from insta_engine.runner import Runner

#: 连续多少个任务被风控拦住就**中止整场**。风控**不会自己消失**（要人工解），
#: 继续跑剩下的任务纯属浪费（实测每轮空跑约 15 分钟）。取 3：偶发单次不中止，成片被拦就停。
MAX_BLOCKED_STREAK = 3


def _risk_blocked(serial: str | None) -> str | None:
    """当前屏幕是否被风控拦住。返回命中的标记，未命中返回 None。

    实测：风控页的文案**在 dump 里可见**（「身份核实」「请依次点击下图图标」），所以可自动识别。
    """
    try:
        xml = DeviceDriver(serial).dump()
    except Exception:  # noqa: BLE001 — 驱动可能抛各种异常；"取不到屏幕"就按**未命中**处理（保守，
        # 宁可漏报风控也不能因为取不到屏幕就判成风控拦截），所以这里刻意不区分异常类型
        return None
    return detect_in_xml(xml)


def summarize(results: list[dict], repeat: int, threshold: float) -> dict:
    """把逐次结果汇总成**可引用的数字**。

    纯函数——**汇总口径是"可引用数字"的核心，必须能离线验证**（不用真机、不用跑一遍）。
    口径（与 一致）：

    - **被风控拦截的 (任务×遍) 排除出所有分母**，但单独计数（`blocked_total` / 逐任务 `blocked`）；
    - 逐任务给"通过遍数 / 有效遍数"，**不满分即标 `unstable`**——"不稳"不等于"失败"，
      但两者都不该被单次结果掩盖；
    - 整体给**逐遍成功率的中位与极差**（对外引用的是这一行，不是某一次）。
    """
    per_task: dict[str, dict] = {}
    for r in results:
        d = per_task.setdefault(r["task"], {"id": r["task"], "valid": 0, "passed": 0, "blocked": 0})
        if r["blocked"] or r.get("env_error"):
            # 环境问题（风控 / 设备断开）一律**排除出分母**——这是上方函数说明的口径。
            # 但要**分开计数**：把"设备断了"说成"被风控"会误导处置方向。
            if r.get("env_error"):
                d["env_error"] = d.get("env_error", 0) + 1
            else:
                d["blocked"] += 1
            continue
        d["valid"] += 1
        d["passed"] += 1 if r["status"] == "passed" else 0

    per_run: list[dict] = []
    for run_idx in range(1, repeat + 1):
        rs = [r for r in results if r["run"] == run_idx and not (r["blocked"] or r.get("env_error"))]
        if not rs:
            continue
        steps = sum(r["total"] for r in rs)
        ok = sum(r["passed"] for r in rs)
        per_run.append({"run": run_idx, "tasks": len(rs), "steps": steps, "passed": ok,
                        "rate": round(ok / steps, 4) if steps else 0.0,
                        "blocked": len([r for r in results if r["run"] == run_idx and r["blocked"]]),
                        "env_error": len([r for r in results
                                          if r["run"] == run_idx and r.get("env_error")])})

    rates = [p["rate"] for p in per_run]
    overall = {"median": round(statistics.median(rates), 4) if rates else 0.0,
               "min": min(rates) if rates else 0.0,
               "max": max(rates) if rates else 0.0,
               "spread_pp": round((max(rates) - min(rates)) * 100, 1) if rates else 0.0}
    # ⚠️ **三态必须分开**（我第一版把 0/N 也标成"不稳"，那是错的）：
    #   - N/N        → 通过
    #   - 0/N        → **稳定失败**（不是 flaky！说明任务写错 或 产品确实不满足断言 —— 值得单独查）
    #   - 0<p<N      → **不稳**（真 flaky，说明用例/环境有随机源）
    # 把两者混成"不稳"，会让人去查"随机性"，而真相往往是"断言写错了"。
    stable_fail = [d["id"] for d in per_task.values() if d["valid"] and d["passed"] == 0]
    unstable = [d["id"] for d in per_task.values()
                if d["valid"] and 0 < d["passed"] < d["valid"]]
    return {"per_task": list(per_task.values()), "per_run": per_run, "overall": overall,
            "unstable": unstable, "stable_fail": stable_fail,
            "blocked_total": len([r for r in results if r["blocked"]]),
            # **靶场指纹**（T76）：数字必须自带"测的是哪个构建"。去重 + 去空。
            # ⚠️ 变体共用 versionName ⇒ 版本号分辨不出 base / v09，指纹才分辨得出。
            "app_versions": sorted({r.get("app_version") or "" for r in results} - {""}),
            "app_fingerprints": sorted({r.get("app_apk_sha256") or "" for r in results} - {""}),
            "verdict": "PASS" if overall["median"] >= threshold else "FAIL"}


def _run_task(task: dict, args, suite: dict, run_idx: int, repeat: int) -> dict:
    """跑一次任务。返回该次的结果（含是否被风控拦住）。

    **重置包名支持按任务覆盖**（`task["_reset_package"]`）——因为**合并多个 App 的套件**时，
    套件级的 `reset_package` 只能是一个包，而每个任务要清的往往是**它自己那个 App**。
    （实测：把 demo-app 与 Tasks.org 合成一个 30 条套件时撞上这个缺口。）
    """
    case = TestCase.model_validate(task["case"])
    pkg = (case.target or {}).get("package")
    reset_pkg = task.get("_reset_package") or suite.get("reset_package")
    if reset_pkg and pkg == reset_pkg:
        adb(args.serial, "shell", "pm", "clear", pkg)
    # repeat=1 时保持原路径（向后兼容）；多次运行按 run 分子目录，否则报告会互相覆盖
    artifacts = Path(args.artifacts) / (task["id"] if repeat == 1 else f"r{run_idx}/{task['id']}")
    # ⚠️ T60：驱动/设备异常必须**就地接住**，不能让它冒到顶层。
    # 实测：USB 瞬断时 `DeviceDriver.__init__` 抛 `AdbError: Can't find any android
    # device/emulator` ⇒ 整个跑分进程崩、`summary.json` 不写 ⇒ **已完成的 83 次数据成孤儿**
    # （60 分钟白跑）。既有护栏只管「**步骤级** env-failure」，管不到"设备整个消失"。
    try:
        drv = DeviceDriver(args.serial)
        run = Runner(drv, artifacts).run(case)
    except Exception as exc:  # noqa: BLE001 —— 驱动异常类型多，统一按"环境问题"处置
        return {"task": task["id"], "run": run_idx, "status": "device-error",
                "passed": 0, "total": len(case.steps), "blocked": None,
                "env_error": f"{type(exc).__name__}: {exc}"}
    render_html(run, artifacts)
    ok = sum(s.status == "passed" for s in run.steps)
    risk = _risk_blocked(args.serial)
    # **靶场指纹**（T76 口径漏洞）：对外数字必须能回答"测的是哪个被测 App 构建"。
    # ⚠️ 光记 `app_version` 不够——所有靶场变体共用 `versionName=1.0`，
    # 分辨不出 base / v09-defect-free，所以再记一个安装包指纹。
    return {"task": task["id"], "run": run_idx, "passed": ok, "total": len(run.steps),
            "status": run.status, "blocked": risk, "artifacts": str(artifacts),
            "app_version": app_version(drv, pkg or ""),
            "app_apk_sha256": app_apk_sha256(drv, pkg or "")}



def _self_test() -> int:
    """离线自检 `summarize` 的**汇总口径**——不需要真机、不需要跑一遍。

    为什么必须有：汇总口径就是"可引用数字"本身。口径错了，**跑得再对也出不了可信的数字**，
    而且错得很隐蔽（中位/极差/分母看起来都正常）。本项目的脚本惯例是带 `--self-test`
    （见 `explore_coverage.py` / `build_page_identity_corpus.py`）。
    """
    def r(task, run, passed, total, status, blocked=None):
        return {"task": task, "run": run, "passed": passed, "total": total,
                "status": status, "blocked": blocked}

    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise SystemExit(f"自检失败：{msg}")

    # ① 全过：中位 100%、无极差、无不稳
    allpass = [r("t1", 1, 5, 5, "passed"), r("t1", 2, 5, 5, "passed")]
    s1 = summarize(allpass, 2, 0.8)
    need(s1["verdict"] == "PASS", "全过应 PASS")
    need(s1["overall"]["median"] == 1.0, f"中位应为 100%，实际 {s1['overall']['median']}")
    need(s1["overall"]["spread_pp"] == 0.0, "全过时极差应为 0")
    need(not s1["unstable"], "全过时不该有「不稳」")

    # ② 不稳：t2 第一遍过、第二遍挂
    #    ⚠️ 口径是**逐遍的「步骤成功率」**，不是「任务通过率」——第一遍 10/10=100%、
    #    第二遍 7/10=70% ⇒ 中位 85%、极差 30pp。（我第一版把期望写成 50%，被自检抓出来了。）
    flaky = [r("t1", 1, 5, 5, "passed"), r("t2", 1, 5, 5, "passed"),
             r("t1", 2, 5, 5, "passed"), r("t2", 2, 2, 5, "failed")]
    s2 = summarize(flaky, 2, 0.8)
    need(s2["overall"]["median"] == 0.85, f"中位应为 85%，实际 {s2['overall']['median']}")
    need(s2["overall"]["spread_pp"] == 30.0, f"极差应为 30pp，实际 {s2['overall']['spread_pp']}")
    need(s2["unstable"] == ["t2"], f"只有 t2 该标不稳，实际 {s2['unstable']}")
    need(s2["verdict"] == "PASS", "中位 85% ≥ 80% ⇒ PASS（单次看会误判，这正是要报中位的原因）")

    # ③ **风控拦截必须排除出分母**（这是最容易被写错、也最影响数字可信度的一条）
    blocked = [r("t1", 1, 5, 5, "passed"), r("t2", 1, 3, 5, "failed", blocked="身份核实"),
               r("t1", 2, 5, 5, "passed"), r("t2", 2, 3, 5, "failed", blocked="身份核实")]
    s3 = summarize(blocked, 2, 0.8)
    need(s3["blocked_total"] == 2, f"应计 2 个被拦截，实际 {s3['blocked_total']}")
    need(s3["overall"]["median"] == 1.0,
         f"t2 全被拦截 ⇒ 分母只剩 t1 ⇒ 中位应为 100%，实际 {s3['overall']['median']}")
    t2 = next(d for d in s3["per_task"] if d["id"] == "t2")
    need(t2["valid"] == 0 and t2["blocked"] == 2, f"t2 应 valid=0/blocked=2，实际 {t2}")
    need("t2" not in s3["unstable"], "全被拦截的任务不该被算成「不稳」（它没跑过）")

    # ④ **0/N 是「稳定失败」，不是「不稳」**——混在一起会让人去查"随机性"，
    #    而真相往往是"断言写错了"。（我第一版就是这么标的，被这条自检的动机暴露出来。）
    stably_failing = [r("t1", 1, 5, 5, "passed"), r("t2", 1, 1, 5, "failed"),
                      r("t1", 2, 5, 5, "passed"), r("t2", 2, 1, 5, "failed")]
    s4 = summarize(stably_failing, 2, 0.8)
    need(s4["stable_fail"] == ["t2"], f"0/2 应进 stable_fail，实际 {s4['stable_fail']}")
    need("t2" not in s4["unstable"], "0/2 不该被算成「不稳」")

    # ⑤ 逐遍的 blocked 计数要按遍分开（不能把两遍的加一起）
    need([p["blocked"] for p in s3["per_run"]] == [1, 1],
         f"逐遍 blocked 应为 [1,1]，实际 {[p['blocked'] for p in s3['per_run']]}")

    # ---- T60：设备/驱动异常（env_error）—— 必须**排除出分母**且**单独计数** ----
    # 实测：USB 瞬断 ⇒ AdbError 从 DeviceDriver.__init__ 抛 ⇒ 整场崩、summary 不写。
    # 修法后它应像风控一样被排除出分母，但**不能被说成风控**（会误导处置方向）。
    env_rows = [
        {"task": "a", "run": 1, "status": "passed", "passed": 3, "total": 3, "blocked": None},
        {"task": "b", "run": 1, "status": "device-error", "passed": 0, "total": 3,
         "blocked": None, "env_error": "AdbError: Can't find any android device/emulator"},
    ]
    sm_env = summarize(env_rows, 1, 0.8)
    need(sm_env["per_run"][0]["steps"] == 3,
         f"设备异常的那条必须**排除出分母**（分母应只剩 3 步）：{sm_env['per_run'][0]}")
    need(sm_env["per_run"][0].get("env_error") == 1,
         f"逐遍应**单独计数** env_error：{sm_env['per_run'][0]}")
    need(sm_env["per_run"][0]["blocked"] == 0,
         "设备异常**不能**被计成风控（否则处置方向会被误导）")
    need(all(d.get("env_error", 0) == 0 for d in sm_env["per_task"] if d["id"] == "a"),
         "正常任务不该被标 env_error")

    print("自检通过：中位/极差、稳定失败与不稳的区分、风控排除出分母、逐遍 blocked 计数、"
          "设备异常排除出分母且单独计数（T60）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="验收跑分（gold DSL 步骤成功率）")
    parser.add_argument("--suite", default="benchmarks/tasks/spike-5-tasks.json",
                        help="任务集路径（相对仓库根；缺省 spike-5）")
    parser.add_argument("--serial", help="设备 serial（缺省取唯一在线设备）")
    parser.add_argument("--artifacts", default="artifacts/spike")
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--repeat", type=int, default=1,
                        help="每条任务重复跑几遍（≥2 时给出中位/极差——**对外引用要用这个**）")
    parser.add_argument("--out", help="把汇总写成 JSON（供报告模板 T34 消费）")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检汇总口径（不需要真机）")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    suite_path = Path(args.suite)
    if not suite_path.is_absolute():
        suite_path = REPO / suite_path
    if not suite_path.exists():
        print(f"找不到任务集: {suite_path}", file=sys.stderr)
        return 2
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    threshold = args.threshold if "--threshold" in sys.argv else float(suite.get("pass_threshold", 0.8))

    tasks = suite["tasks"]
    repeat = max(1, args.repeat)
    print(f"套件 {suite_path.name} · {len(tasks)} 条任务 × {repeat} 遍"
          f"（共 {sum(len(t['case']['steps']) for t in tasks) * repeat} 步）\n")

    results: list[dict] = []
    blocked_streak = 0
    aborted = False
    for run_idx in range(1, repeat + 1):
        if aborted:
            break
        for task in tasks:
            r = _run_task(task, args, suite, run_idx, repeat)
            results.append(r)
            if r.get("env_error"):
                # 设备没了 ⇒ 继续跑剩下的只会全是同样的错。**立刻中止**，
                # 让后面的汇总照常执行（这才是 T60 的要点：崩了也要留下已完成部分的数字）。
                aborted = True
                print(f"\n⛔ **跑分中断**：{r['env_error']}")
                print("   这是**环境问题**（设备断开/驱动异常），不是产品失败。")
                print("   ⇒ 已完成的轮次仍会汇总；**本轮不完整**，引用时须说明。")
                break
            if r["blocked"]:
                blocked_streak += 1
                print(f"  r{run_idx} {r['task']}: ⛔ 风控拦截（「{r['blocked']}」）")
                if blocked_streak >= MAX_BLOCKED_STREAK:
                    aborted = True
                    print(f"\n⛔ 连续 {blocked_streak} 个任务被风控拦住 —— **中止整场**。")
                    print("   风控**不会自己消失**（要人工解掉），继续跑剩下的任务纯属浪费。")
                    print(" 处置：自有 App 在测试环境关风控/加白；第三方 App 无法绕过。")
                    break
                continue
            blocked_streak = 0
            print(f"  r{run_idx} {r['task']}: {r['status']}（步骤 {r['passed']}/{r['total']}）")

    # ---- 汇总（口径收在 summarize 里，可离线验证）----
    sm = summarize(results, repeat, threshold)
    per_task, per_run = sm["per_task"], sm["per_run"]
    median, overall = sm["overall"]["median"], sm["overall"]
    unstable = sm["unstable"]
    stable_fail, blocked_total, verdict = sm["stable_fail"], sm["blocked_total"], sm["verdict"]

    print(f"\n=== 逐任务通过分布（{repeat} 遍）===")
    for d in per_task:
        if d["valid"] == 0:
            print(f"  ⛔ {d['id']:<30} 全被风控拦截（{d['blocked']} 遍）")
            continue
        if d["passed"] == d["valid"]:
            flag = "✅"
        elif d["passed"] == 0:
            flag = "❌ 稳定失败"
        else:
            flag = "⚠️ 不稳"
        extra = f" · 风控 {d['blocked']}" if d["blocked"] else ""
        print(f"  {flag} {d['id']:<30} {d['passed']}/{d['valid']} 遍通过{extra}")

    print("\n=== 逐遍步骤成功率 ===")
    for p in per_run:
        print(f"  r{p['run']}: {p['passed']}/{p['steps']} = {p['rate']:.0%}"
              f"（任务 {p['tasks']} 条，风控拦截 {p['blocked']} 条）")

    print(f"\n结论: {verdict}")
    if repeat > 1:
        print(f"  **中位 {median:.0%} · 极差 {overall['min']:.0%}–{overall['max']:.0%}"
              f"（{overall['spread_pp']:.1f}pp）**  套件 {suite_path.name}")
        print("  ⇒ **对外只引用这一行**（中位 + 极差 + 样本量），不要引单次数字。")
    else:
        print(f"  步骤成功率 {median:.0%}（阈值 {threshold:.0%}）  套件 {suite_path.name}")
        print("  ⚠️ **这是单次结果，不可对外引用**——加 `--repeat 3` 拿到中位与极差再引用"
              "（实测单次结果不可靠：同一批任务三次 89%/96%/92%，失败集合每次不同）。")

    # **靶场指纹**（T76）：数字要自带"测的是哪个构建"。⚠️ 靶场变体共用 `versionName`，
    # 所以版本号之外还要印指纹——否则"这个数字在哪个靶场上测的"答不上来。
    if sm.get("app_versions") or sm.get("app_fingerprints"):
        print(f"  靶场: 版本 {', '.join(sm.get('app_versions') or ['?'])}"
              f" · 指纹 {', '.join(sm.get('app_fingerprints') or ['?'])}")

    if stable_fail:
        print(f"\n❌ **{len(stable_fail)} 条任务「稳定失败」**（{repeat} 遍全挂）——**不是 flaky**，"
              "说明任务写错 或 产品确实不满足断言，**值得单独查**：")
        print(f"    {'、'.join(stable_fail)}")
    if unstable:
        print(f"\n⚠️ **{len(unstable)} 条任务「不稳」**（有的遍过、有的遍挂）——这是真 flaky，"
              "说明用例或环境有随机源：")
        print(f"    {'、'.join(unstable)}")
    if blocked_total:
        print(f"\n⚠️ **另有 {blocked_total} 个「任务×遍」被风控拦截，已排除出分母**"
              "（不是产品失败，也不能算通过）：")
        for r in results:
            if r["blocked"]:
                print(f"    ⛔ r{r['run']} {r['task']}（{r['blocked']}）")
        print("   ⇒ 引用时**必须一并说明**被拦截的数量——否则读者会以为这些任务都过了。")
    env_error_total = sum(1 for r in results if r.get("env_error"))
    if aborted:
        print("\n⚠️ 本场**被中止**（风控连续拦截 或 设备断开），"
              "上面的数字只覆盖中止前的部分，样本量不足。")
    if env_error_total:
        print(f"\n⚠️ **{env_error_total} 个「任务×遍」因设备/驱动异常未完成**（已排除出分母）——"
              "这是**环境问题**，不是产品失败。")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "suite": suite_path.name, "repeat": repeat, "threshold": threshold,
            "aborted": aborted, "verdict": verdict, "overall": overall, "per_run": per_run,
            "per_task": per_task, "unstable": unstable, "stable_fail": stable_fail,
            "blocked_total": blocked_total,
            "env_error_total": env_error_total,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n汇总已写 {out.resolve()}")

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
