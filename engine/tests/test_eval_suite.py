"""eval_suite：评测口径锁定 + 基线对比判定（与 spike_check 口径同源，改动须双向同步）。"""

from __future__ import annotations

import json

import pytest

from insta_engine.eval_suite import compare, summarize, summary_line


def _r(task: str, run: int, status: str, passed: int, total: int,
       blocked: str | None = None, env_error: str | None = None,
       version: str = "1.0", fp: str = "fp1") -> dict:
    return {"task": task, "run": run, "status": status, "passed": passed,
            "total": total, "blocked": blocked, "env_error": env_error,
            "app_version": version, "app_apk_sha256": fp}


def test_summarize_three_states_are_distinct():
    """三态分开（spike_check 口径）：N/N 通过、0/N 稳定失败、0<p<N 不稳——不许混。"""
    rs = [_r("a", 1, "passed", 3, 3), _r("a", 2, "passed", 3, 3),          # a: 稳过
          _r("b", 1, "failed", 1, 3), _r("b", 2, "failed", 1, 3),          # b: 稳定失败
          _r("c", 1, "passed", 3, 3), _r("c", 2, "failed", 0, 3)]          # c: 不稳
    sm = summarize(rs, 2, 0.8)
    by_id = {d["id"]: d for d in sm["per_task"]}
    assert by_id["a"] == {"id": "a", "passed": 2, "valid": 2, "blocked": 0}
    assert sm["stable_fail"] == ["b"] and sm["unstable"] == ["c"]


def test_summarize_risk_blocked_excluded_from_denominator():
    """风控拦截的遍**排除出分母**（不算失败），且单列 blocked——处置方向不被误导。"""
    rs = [_r("a", 1, "passed", 3, 3),
          _r("a", 2, "failed", 0, 3, blocked="请依次点击下图图标")]
    sm = summarize(rs, 2, 0.8)
    assert sm["per_task"][0]["valid"] == 1 and sm["per_task"][0]["passed"] == 1
    assert sm["per_run"][1]["blocked"] == 1 and sm["per_run"][1]["steps"] == 0
    assert sm["blocked_total"] == 1 and sm["stable_fail"] == []


def test_summarize_env_error_counted_separately():
    """设备异常单列 env_error，不计入分母也不能混成风控（T60）。"""
    rs = [_r("a", 1, "passed", 3, 3),
          _r("a", 2, "device-error", 0, 3, env_error="AdbError: gone")]
    sm = summarize(rs, 2, 0.8)
    assert sm["per_run"][1]["env_error"] == 1
    assert sm["per_run"][1]["steps"] == 0 and sm["per_run"][1]["blocked"] == 0


def test_summarize_median_spread_and_fingerprint():
    rs = [_r("a", 1, "passed", 4, 4), _r("a", 2, "passed", 4, 4), _r("a", 3, "passed", 4, 4),
          _r("b", 1, "failed", 2, 4), _r("b", 2, "passed", 4, 4), _r("b", 3, "passed", 4, 4)]
    sm = summarize(rs, 3, 0.8)
    # 逐遍比率:r1=(4+2)/8=0.75、r2=r3=1.0——中位 1.0、极差 25pp
    assert sm["overall"]["median"] == 1.0 and sm["overall"]["min"] == 0.75
    assert sm["overall"]["spread_pp"] == 25.0
    assert sm["app_versions"] == ["1.0"] and sm["app_fingerprints"] == ["fp1"]


def _report(median: float, tasks: dict[str, tuple[int, int]], fp: str = "fp1") -> dict:
    return {"schema": "mat.eval/1", "overall": {"median": median},
            "per_task": [{"id": k, "passed": v[0], "valid": v[1]}
                         for k, v in tasks.items()],
            "app_fingerprints": [fp]}


def test_compare_median_regression_fails():
    base = _report(1.0, {"a": (3, 3), "b": (3, 3)})
    cur = _report(0.93, {"a": (3, 3), "b": (3, 3)})
    out = compare(cur, base)
    assert out["verdict"] == "FAIL" and any("中位通过率退化" in r for r in out["regressions"])


def test_compare_new_stable_fail_and_flaky_are_regressions():
    base = _report(1.0, {"a": (3, 3), "b": (3, 3), "c": (3, 3)})
    cur = _report(0.8, {"a": (0, 3), "b": (1, 3), "c": (3, 3)})
    out = compare(cur, base)
    assert out["verdict"] == "FAIL"
    assert any("a: 通过 → 稳定失败" in r for r in out["regressions"])
    assert any("b: 新增不稳" in r for r in out["regressions"])


def test_compare_improvement_and_fingerprint_note():
    base = _report(0.8, {"a": (0, 3)}, fp="fp-old")
    cur = _report(1.0, {"a": (3, 3)}, fp="fp-new")
    out = compare(cur, base)
    assert out["verdict"] == "PASS"
    assert any("a: 稳定失败 → 通过" in i for i in out["improvements"])
    assert out["app_fingerprints_changed"] and "构建" in out["note"]


def test_compare_small_drift_within_threshold_passes():
    base = _report(1.0, {"a": (3, 3)})
    cur = _report(0.97, {"a": (3, 3)})   # 3pp < 默认 5pp 阈值
    assert compare(cur, base)["verdict"] == "PASS"


def test_compare_requires_matching_task_ids():
    """基线里没有的任务不参与判定（新任务待下一轮成为基线后再守）。"""
    base = _report(1.0, {"a": (3, 3)})
    cur = _report(1.0, {"a": (3, 3), "new": (0, 3)})   # 新任务失败,但中位相同
    out = compare(cur, base)
    assert out["verdict"] == "PASS", "新增任务的失败不判回归（无历史基线）"


@pytest.mark.parametrize("suite_threshold,given,expect", [(0.8, 0.79, "FAIL"), (0.8, 0.8, "PASS")])
def test_summarize_verdict_threshold(suite_threshold, given, expect):
    rs = [_r("a", 1, "passed", 79, 100)] if given < 0.8 else [_r("a", 1, "passed", 80, 100)]
    assert summarize(rs, 1, suite_threshold)["verdict"] == expect


def _report_for_line(**over) -> dict:
    r = {"schema": "mat.eval/1", "repeat": 3, "aborted": False, "blocked_total": 0,
         "unstable": [], "app_fingerprints": ["fp1"],
         "per_task": [{"id": "a"}, {"id": "b"}],
         "overall": {"median": 1.0, "min": 1.0, "max": 1.0, "spread_pp": 0.0}}
    r.update(over)
    return r


def test_summary_line_zero_spread_uses_integer_percent():
    """精度跟极差走：spread 0 ⇒ 整数百分比（否则 100% 与 99.7% 并存会自相矛盾）。"""
    line = summary_line(_report_for_line())
    assert "中位 100%" in line and "极差 100%–100%（0.0pp）" in line
    line2 = summary_line(_report_for_line(overall={"median": 0.997, "min": 0.97,
                                                   "max": 1.0, "spread_pp": 3.0}))
    assert "中位 99.7%" in line2 and "97.0%–100.0%（3.0pp）" in line2


def test_summary_line_carries_caveats_inline():
    """不稳/风控/不完整必须与数字**同句**出现——引用限制不许单独成段被忽略。"""
    line = summary_line(_report_for_line(unstable=["tx-a01"], blocked_total=2,
                                         aborted=True))
    for kw in ("不稳 1 条（tx-a01）", "单遍结果不可引用", "风控拦截（已排除出分母）",
               "不完整"):
        assert kw in line


def _r_with_extras(task: str, run: int, status: str, passed: int, total: int, *,
                   launch_ms: int | None = None, mem: float | None = None,
                   heal_t: int = 0, heal_r: int = 0,
                   categories: dict | None = None) -> dict:
    r = _r(task, run, status, passed, total)
    r["perf"] = {"launch_ms": launch_ms, "mem_pss_mb": mem}
    r["heal_triggered"], r["heal_recovered"] = heal_t, heal_r
    r["categories"] = categories or {}
    return r


def test_summarize_heal_rate_is_recovery_of_triggered():
    """G7 口径：触发兜底的步骤中**该步最终转过**的比例；无触发 ⇒ rate None。"""
    rs = [_r_with_extras("a", 1, "passed", 3, 3, heal_t=2, heal_r=1),
          _r_with_extras("b", 1, "passed", 2, 2, heal_t=1, heal_r=1)]
    sm = summarize(rs, 1, 0.8)
    assert sm["overall"]["heal"] == {"triggered": 3, "recovered": 2, "rate": round(2 / 3, 4)}
    sm0 = summarize([_r("a", 1, "passed", 3, 3)], 1, 0.8)
    assert sm0["overall"]["heal"] == {"triggered": 0, "recovered": 0, "rate": None}


def test_summarize_perf_median_ignores_missing():
    rs = [_r_with_extras("a", 1, "passed", 3, 3, launch_ms=1000, mem=200.0),
          _r_with_extras("b", 1, "passed", 3, 3, launch_ms=2000, mem=None),
          _r("c", 1, "failed", 0, 3)]
    sm = summarize(rs, 1, 0.8)
    assert sm["overall"]["perf"] == {"launch_ms_median": 1500, "mem_pss_mb_median": 200.0}


def test_compare_perf_regression_and_fingerprint_skip():
    base = _report(1.0, {"a": (3, 3)})
    base["overall"]["perf"] = {"launch_ms_median": 1000, "mem_pss_mb_median": 200.0}
    cur = _report(1.0, {"a": (3, 3)})
    cur["overall"]["perf"] = {"launch_ms_median": 1400, "mem_pss_mb_median": 280.0}
    out = compare(cur, base)
    assert out["verdict"] == "FAIL"
    assert any("启动耗时 劣化 40%" in r for r in out["regressions"])
    assert any("内存 PSS 劣化 40%" in r for r in out["regressions"])
    # App 升级（指纹变化）⇒ 性能不可比，跳过守门
    cur2 = _report(1.0, {"a": (3, 3)}, fp="fp-new")
    cur2["overall"]["perf"] = cur["overall"]["perf"]
    out2 = compare(cur2, base)
    assert out2["verdict"] == "PASS" and not any("劣化" in r for r in out2["regressions"])
    # 旧基线无 perf 段 ⇒ 跳过（向后兼容）
    base_old = _report(1.0, {"a": (3, 3)})
    assert compare(cur, base_old)["verdict"] == "PASS"


def test_summarize_per_task_perf_median():
    """per_task 带性能中位：单任务趋势要能看，缺样本的任务不挂 perf 段（不伪造 0）。"""
    rs = [_r_with_extras("a", 1, "passed", 3, 3, launch_ms=1000, mem=200.0),
          _r_with_extras("a", 2, "passed", 3, 3, launch_ms=1200, mem=220.0),
          _r_with_extras("b", 1, "passed", 2, 2, launch_ms=None, mem=None)]
    sm = summarize(rs, 2, 0.8)
    by_id = {d["id"]: d for d in sm["per_task"]}
    assert by_id["a"]["perf"] == {"launch_ms_median": 1100,
                                  "mem_pss_mb_median": 210.0, "samples": 2}
    assert "perf" not in by_id["b"], "没有采集样本就不该有 perf 段"


def test_summarize_crash_counts_task_runs_and_summary_line():
    """崩溃维度：报告要能回答「这轮有没有真崩溃」——按任务×遍计，同名任务不去重。"""
    rs = [_r_with_extras("a", 1, "failed", 1, 3, categories={"app-crash": 1}),
          _r_with_extras("a", 2, "failed", 1, 3, categories={"app-crash": 2}),
          _r_with_extras("b", 1, "failed", 0, 3, categories={"locate-fail": 1})]
    sm = summarize(rs, 2, 0.8)
    assert sm["overall"]["crash"] == {"task_runs": 2, "tasks": ["a"],
                                      "runs": ["a#r1", "a#r2"]}
    line = summary_line({"repeat": 2, "per_task": sm["per_task"],
                         "app_fingerprints": [], "overall": sm["overall"]})
    assert "崩溃 2 个「任务×遍」（a）" in line
    # 无崩溃时也要给 0——不能让人把"字段缺失"读成"没测"
    sm0 = summarize([_r("c", 1, "passed", 3, 3)], 1, 0.8)
    assert sm0["overall"]["crash"] == {"task_runs": 0, "tasks": [], "runs": []}


def test_compare_new_crash_is_note_not_regression():
    """新增崩溃是**产品信号**，进 note 让人确认；不自动判 FAIL（App 升级也会变）。"""
    base = _report(1.0, {"a": (3, 3)})
    base["overall"]["crash"] = {"task_runs": 0, "tasks": []}
    cur = _report(1.0, {"a": (3, 3)})
    cur["overall"]["crash"] = {"task_runs": 1, "tasks": ["a"]}
    out = compare(cur, base)
    assert out["verdict"] == "PASS"
    assert "新增崩溃任务 1 个（a）" in out["note"]



def test_first_run_summarize_k_n_and_false_red(tmp_path):
    """首跑通过率统计口径（单一实现）:k/n 聚合 + 假红剔除。"""
    from insta_engine.eval_first_run import summarize

    for i, (status, false_red) in enumerate(
            [("passed", False), ("failed", True), ("failed", False)]):
        d = tmp_path / f"r{i}"
        d.mkdir()
        (d / "run.json").write_text(json.dumps({
            "case": {"target": {"package": "com.x"}},
            "run": {"status": status, "case_name": "登录冒烟", "duration_ms": 1000,
                    "steps": [{"step_id": "s1", "status": "failed",
                               "error": "AssertionError: 断言失败",
                               "category": "assert-fail", "false_red": false_red,
                               "screenshot": None, "logcat_file": None}]}}),
            encoding="utf-8")
    sm = summarize([tmp_path])
    assert sm["total"] == 3 and sm["passed"] == 1
    assert sm["false_red"] == 1
    # 剔假红:通过率 = 1/2 = 0.5(测量噪声不压低数字)
    assert sm["d3_ex_false_red"] == 0.5
    # k/n:同用例 3 次聚合,单次结果不可引用
    assert sm["per_case"][0]["n"] == 3 and sm["per_case"][0]["passed"] == 1
    # 失败分层:断言失败被识别
    assert any(r["kind"] == "断言" for r in sm["rows"] if r["status"] != "passed")


def test_eval_gen_alias_saves_metrics_report(tmp_path, monkeypatch):
    """`mat eval gen`（生成首穿率的口语名）必须与 first-run 同实现：产出可被 metrics 消费。"""
    from insta_engine.__main__ import main

    monkeypatch.setattr("insta_engine.__main__._load_env_file", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "batch" / "caseA"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "case": {"target": {"package": "com.x"}},
        "run": {"case_id": "caseA", "case_name": "登录冒烟", "status": "passed",
                "duration_ms": 1000, "steps": []},
    }, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SystemExit) as ei:
        main(["eval", "gen", str(tmp_path / "batch"), "--save"])
    assert ei.value.code == 0
    saved = tmp_path / "artifacts" / "eval" / "first-run-report.json"
    assert saved.exists(), "cli 必须落盘 first-run-report.json（metrics 卡的数据源）"
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["total"] == 1 and data["passed"] == 1 and data["per_case"][0]["k_ratio"] == 1.0

