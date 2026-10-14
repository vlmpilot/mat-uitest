"""run_summary 摘要器：run.json → ≤40 行判读文本（skill 教 agent 用它替代整读 report.html）。"""

import json

from insta_engine.run_summary import MAX_LINES, digest, self_test, summarize_run


def _write_run(tmp_path, run, case=None):
    run_dir = tmp_path / "case_x"
    run_dir.mkdir()
    data = {"finished_at": "2026-10-03T00:00:00+00:00", "device_serial": "EMU-51",
            "case": case or {"caseId": "c1", "name": "样例"},
            "run": run}
    (run_dir / "run.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return run_dir


def test_failed_run_digest_carries_category_error_and_evidence(tmp_path):
    run = {
        "case_name": "样例", "status": "failed", "step_success_rate": 0.5,
        "duration_ms": 12000, "failed_categories": {"locate-fail": 1},
        "steps": [
            {"step_id": "s1", "intent": "启动", "status": "passed", "category": None},
            {"step_id": "s2", "intent": "点登录", "status": "failed", "category": "locate-fail",
             "error": "LocateFailed: 定位失败（策略链）: res-id=login_btn 未命中\n第二行",
             "screenshot": "screenshots/s2.png", "logcat_file": "logs/s2.logcat.txt"},
        ],
    }
    run_dir = _write_run(tmp_path, run)
    summary = summarize_run(run_dir)
    assert summary["status"] == "failed" and summary["total_steps"] == 2
    text = digest(summary)
    assert "样例" in text and "50%" and "locate-fail" in text
    assert "s2" in text and "LocateFailed" in text          # 错误只取首行
    assert "第二行" not in text
    assert "screenshots/s2.png" in text and "logs/s2.logcat.txt" in text
    assert "mat diagnose" in text and "need_human" in text
    assert "15MB" in text                                    # 明示勿整读 HTML
    assert len(text.splitlines()) <= MAX_LINES


def test_env_failure_and_truncation(tmp_path):
    steps = [{"step_id": f"s{i}", "intent": f"步骤{i}", "status": "failed",
              "category": "env-failure", "error": f"adb 掉线 {i}"} for i in range(12)]
    run = {"case_name": "x", "status": "failed", "step_success_rate": 0.0,
           "duration_ms": 99999, "failed_categories": {"env-failure": 12}, "steps": steps}
    run_dir = _write_run(tmp_path, run)
    text = digest(summarize_run(run_dir))
    assert "不计入产品失败" in text            # env-failure 诚实标注
    assert "其余 4 个失败步骤略" in text        # 超过 8 个折叠
    assert len(text.splitlines()) <= MAX_LINES


def test_passed_run_is_short_and_self_test_passes():
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = pathlib.Path(tmp) / "r"
        run_dir.mkdir()
        (run_dir / "run.json").write_text(json.dumps({
            "device_serial": "EMU-51", "case": {"caseId": "c", "name": "全过"},
            "run": {"case_name": "全过", "status": "passed", "step_success_rate": 1.0,
                    "duration_ms": 8000, "steps": [
                        {"step_id": "s1", "intent": "启动", "status": "passed", "category": None}]},
        }, ensure_ascii=False), encoding="utf-8")
        text = digest(summarize_run(run_dir))
        assert "无失败步骤" in text and "全过" in text
    assert self_test() == 0
