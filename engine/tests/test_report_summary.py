"""P0-5：报告结论页与 summary.md（给经理/客户/Agent 读的一页结论）。"""

from __future__ import annotations

from insta_engine.report import render_html, render_summary_md
from insta_engine.runner import RunResult, StepResult


def _run(status="failed", skipped=0, failed_cats=None):
    steps = [StepResult(step_id="s1", intent="启动", action="launch", status="passed",
                        screenshot="screenshots/s1.png", duration_ms=100),
             StepResult(step_id="s2", intent="点登录", action="tap", status="failed",
                        category="locate-fail", error="找不到控件",
                        screenshot="screenshots/s2.png",
                        logcat_file="logs/s2.logcat.txt", duration_ms=300)]
    for i in range(skipped):
        steps.append(StepResult(step_id=f"s{3 + i}", intent="后续步骤", action="tap",
                                status="skipped", error="abort 后未执行"))
    return RunResult(case_id="c1", case_name="登录回归", status=status,
                     step_success_rate=0.5, duration_ms=3200,
                     failed_categories=failed_cats or {"locate-fail": 1},
                     steps=steps)


def test_summary_md_contains_verdict_and_untested(tmp_path):
    md = render_summary_md(_run(skipped=1))
    assert "# MAT 执行结论 — 登录回归 — 未通过" in md
    assert "1 通过 / 1 失败 / 1 未执行" in md
    assert "## 未测/未执行" in md and "`s3`" in md
    assert "失败步系统日志 1/1" in md
    assert "连同本清单一起说" in md


def test_summary_md_no_skipped_note():
    md = render_summary_md(_run())
    assert "不存在「没测就下结论」" in md


def test_report_html_contains_summary_block(tmp_path):
    out = render_html(_run(), tmp_path)
    html = out.read_text(encoding="utf-8")
    assert "id='summary'" in html
    assert "未通过" in html and "class='vicon bad'" in html
    assert "证据覆盖" in html and "locate-fail" in html
    assert (tmp_path / "summary.md").is_file()


def test_report_html_skipped_section(tmp_path):
    html = render_html(_run(skipped=2), tmp_path).read_text(encoding="utf-8")
    assert "未测/未执行（2）" in html and "s3" in html and "s4" in html
