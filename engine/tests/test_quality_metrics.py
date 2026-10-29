"""质量指标卡：全部从既有产物推导；缺产出如实标「未建立」。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from insta_engine.quality_metrics import build_metrics, render_text


def _mk_workspace(tmp_path: Path) -> None:
    (tmp_path / "artifacts" / "eval").mkdir(parents=True)
    (tmp_path / "artifacts" / "page_index").mkdir(parents=True)
    (tmp_path / "artifacts" / "usage.json").write_text(
        json.dumps({"2026-10": {"gen_count": 13.0, "gen_limit": 60}}), encoding="utf-8")


def test_baseline_metric_and_caveat(tmp_path):
    """评测基线 → 步骤成功率指标，口径（条数×遍数/指纹/不稳）同句出现。"""
    _mk_workspace(tmp_path)
    baseline = {"schema": "mat.eval/1", "suite": "m1-thirdparty.json", "repeat": 3,
                "overall": {"median": 1.0, "min": 0.986, "max": 1.0, "spread_pp": 1.4},
                "per_task": [{"id": f"t{i}"} for i in range(20)],
                "unstable": ["tx-f01"], "app_fingerprints": ["159597cc"]}
    (tmp_path / "artifacts" / "eval" / "baseline-m1-thirdparty.json").write_text(
        json.dumps(baseline), encoding="utf-8")
    m = build_metrics(tmp_path)
    item = next(i for i in m["products"] if "步骤成功率" in i["name"])
    assert item["value"] == "100%"
    assert "20 条 × 3 遍" in item["caveat"] and "159597cc" in item["caveat"] \
        and "不稳 1 条" in item["caveat"]


def test_no_baseline_reports_unbuilt(tmp_path):
    """没有基线 ⇒ 指标缺位如实呈现，不编 0。"""
    _mk_workspace(tmp_path)
    m = build_metrics(tmp_path)
    assert not [i for i in m["products"] if "步骤成功率" in i["name"]]
    tool = {i["name"]: i["value"] for i in m["tool"]}
    assert tool["生成首穿率"] == "未建立"


def test_first_run_report_feeds_metric_and_target(tmp_path):
    """`mat eval gen --save` 的产物 → 生成首穿率指标转正（不再是「未建立」）。"""
    _mk_workspace(tmp_path)
    report = tmp_path / "artifacts" / "eval" / "first-run-report.json"
    report.write_text(json.dumps({
        "total": 21, "passed": 7, "false_red": 0, "d3_ex_false_red": 0.333,
        "per_case": [{"id": f"c{i}"} for i in range(21)],
        "saved_at": "2026-10-26T10:00:00+00:00",
    }), encoding="utf-8")
    item = next(i for i in build_metrics(tmp_path)["tool"] if "生成首穿率" in i["name"])
    assert item["value"] == "7/21 = 33%"
    assert item["met"] is False, "33% < 70% 目标 ⇒ 未达标（✗ 而不是不判）"
    assert item["target"] == "≥ 70%（暂定）" and "2026-10-26" in item["caveat"]

    report.write_text(json.dumps({
        "total": 10, "passed": 9, "false_red": 0, "d3_ex_false_red": 0.9,
        "per_case": [{"id": "c1"}], "saved_at": "2026-10-27T10:00:00+00:00",
    }), encoding="utf-8")
    item2 = next(i for i in build_metrics(tmp_path)["tool"] if "生成首穿率" in i["name"])
    assert item2["value"] == "9/10 = 90%" and item2["met"] is True


def test_baseline_perf_feeds_launch_line(tmp_path):
    """基线带 perf 段时，卡上必须有「启动耗时」——数据现成，不该只留在 JSON 里。"""
    _mk_workspace(tmp_path)
    (tmp_path / "artifacts" / "eval" / "baseline-demo.json").write_text(json.dumps({
        "schema": "mat.eval/1", "suite": "demo.json", "repeat": 1,
        "overall": {"median": 1.0, "min": 1.0, "max": 1.0, "spread_pp": 0.0,
                    "perf": {"launch_ms_median": 1630, "mem_pss_mb_median": 210.0}},
        "per_task": [{"id": "t1"}], "unstable": [], "app_fingerprints": ["fp1"],
    }), encoding="utf-8")
    item = next(i for i in build_metrics(tmp_path)["products"] if "启动耗时" in i["name"])
    assert "1630 ms" in item["value"] and "demo.json" in item["value"]
    assert "30%" in item["caveat"], "口径要带上 compare 的劣化守门阈值"


def test_crash_ratio_from_runs(tmp_path):
    """崩溃占比从 list_runs 的 failed_categories 推导。"""
    _mk_workspace(tmp_path)
    runs_dir = tmp_path / "artifacts" / "r1"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run.json").write_text(json.dumps({
        "run": {"status": "failed", "step_success_rate": 0.5,
                "failed_categories": {"app-crash": 1},
                "steps": [{"step_id": "s1", "status": "failed"}]}}), encoding="utf-8")
    m = build_metrics(tmp_path)
    item = next(i for i in m["products"] if "崩溃" in i["name"])
    assert item["value"] == "1/1"


def test_heal_rate_from_recent_runs(tmp_path):
    """G7 自愈率：触发兜底的步骤中最终转过的比例（从最近 run.json 统计）。"""
    _mk_workspace(tmp_path)
    for i, (heal, status) in enumerate([({"original": "a", "match": "b"}, "passed"),
                                        ({"original": "c", "match": "d"}, "failed")]):
        d = tmp_path / "artifacts" / f"r{i}"
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({
            "run": {"status": "passed", "steps": [
                {"step_id": "s1", "status": status, "heal": heal}]}}), encoding="utf-8")
    m = build_metrics(tmp_path)
    item = next(i for i in m["tool"] if "自愈恢复率" in i["name"])
    assert item["value"] == "50%"
    assert "触发兜底 2 步" in item["caveat"]


def _mk_run(root: Path, name: str, status: str, steps: list[dict]) -> None:
    d = root / "artifacts" / name
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "run": {"status": status, "steps": steps}}), encoding="utf-8")


def test_heal_rate_is_not_judged_on_tiny_sample(tmp_path):
    """n=2 不许打 ✓ 达标——实测卡上「100% [✓ 达标]」的分母就是 2 步。"""
    _mk_workspace(tmp_path)
    for i in range(2):
        _mk_run(tmp_path, f"r{i}", "passed",
                [{"step_id": "s1", "status": "passed", "heal": {"via": "vision-llm"}}])
    item = next(i for i in build_metrics(tmp_path)["tool"] if "自愈恢复率（步级" in i["name"])
    assert item["value"] == "100%"
    assert item["met"] is None, "样本不足时只报数，不判达标"
    assert "样本不足（n=2 < 10）" in item["caveat"]


def test_heal_case_level_reported_apart_from_step_level(tmp_path):
    """步级与用例级是两个量：步都转过了，用例仍然全挂（实测 2/2 vs 0/3）。"""
    _mk_workspace(tmp_path)
    for i in range(3):
        _mk_run(tmp_path, f"r{i}", "failed",
                [{"step_id": "s3", "status": "passed", "heal": {"via": "vision-llm"}},
                 {"step_id": "s5", "status": "failed"}])
    m = build_metrics(tmp_path)
    step = next(i for i in m["tool"] if "自愈恢复率（步级" in i["name"])
    case = next(i for i in m["tool"] if "自愈恢复率（用例级" in i["name"])
    assert step["value"] == "100%"
    assert case["value"] == "0/3 = 0%"
    assert case["met"] is None and case["target"] is None, \
        "用例级不设目标：损伤场景 0/3 是结构性结果，拿 ≥80% 判它只会得到一个不可修的红字"
    assert "级联失败" in case["caveat"]


def test_heal_via_breakdown_flags_local_fuzzy_as_defect(tmp_path):
    """local-fuzzy 命中 = 原定位值不精确（缺陷信号），必须在卡上说清，不能混成能力。"""
    _mk_workspace(tmp_path)
    _mk_run(tmp_path, "r1", "passed",
            [{"step_id": "s2", "status": "passed",
              "heal": {"via": "local-fuzzy", "original": "description=消息"}}])
    _mk_run(tmp_path, "r2", "passed",
            [{"step_id": "s3", "status": "passed", "heal": {"via": "vision-llm"}}])
    item = next(i for i in build_metrics(tmp_path)["tool"] if "自愈恢复率（步级" in i["name"])
    assert "via: " in item["caveat"]
    assert "local-fuzzy 1" in item["caveat"]
    assert "原定位值不精确" in item["caveat"]


def test_assertion_strength_line_counts_skeleton(tmp_path):
    """断言强度那行要能看出「骨架断言」有多少——它是只证明页面存在的假绿形态。"""
    _mk_workspace(tmp_path)
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "c.json").write_text(json.dumps({"name": "搜索", "steps": [
        {"id": "s3", "action": "input",
         "target": {"strategy": "text", "value": "搜索框"}, "params": {"text": "咖啡"}},
        {"id": "s5", "action": "wait",
         "assertions": [{"type": "exists",
                         "target": {"strategy": "text", "value": "已选中综合"}}]},
    ]}), encoding="utf-8")
    item = next(i for i in build_metrics(tmp_path)["tool"] if "断言强度" in i["name"])
    assert "骨架 1" in item["caveat"], item["caveat"]


def test_no_heal_keeps_single_unbuilt_line(tmp_path):
    """没触发过兜底 ⇒ 只留步级一行「未建立」，不凭空多出一条用例级空行。"""
    _mk_workspace(tmp_path)
    _mk_run(tmp_path, "r1", "passed", [{"step_id": "s1", "status": "passed"}])
    lines = [i for i in build_metrics(tmp_path)["tool"] if "自愈恢复率" in i["name"]]
    assert len(lines) == 1 and lines[0]["value"] == "未建立"
    assert lines[0]["met"] is None


def test_render_text_groups():
    m = build_metrics(Path(tempfile.mkdtemp()))
    text = render_text(m)
    for title in ("产品质量", "工具能力", "成本效率"):
        assert title in text


def test_targets_are_judged_inline(tmp_path):
    """指标卡带 target 与达标判定:达标 ✓、未达 ✗、未建立不判。"""
    _mk_workspace(tmp_path)
    baseline = {"schema": "mat.eval/1", "suite": "s.json", "repeat": 1,
                "overall": {"median": 0.97, "min": 0.97, "max": 0.97, "spread_pp": 0.0},
                "per_task": [{"id": "a"}], "unstable": [], "app_fingerprints": []}
    (tmp_path / "artifacts" / "eval" / "baseline-s.json").write_text(
        json.dumps(baseline), encoding="utf-8")
    m = build_metrics(tmp_path)
    all_items = {i["name"]: i for i in m["products"] + m["tool"]}
    ok_item = next(v for k, v in all_items.items() if "步骤成功率" in k)
    assert ok_item["met"] is True and ok_item["target"] == "≥ 95%"
    heal_item = next(v for k, v in all_items.items() if "自愈恢复率" in k)
    assert heal_item["met"] is None, "未建立(无触发数据)不判达标"
    text = render_text(m)
    assert "[✓ 达标]" in text and "（目标 ≥ 95%）" in text
