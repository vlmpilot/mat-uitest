"""run.json → 运行摘要（≤40 行文本，供 AI agent 与 CI 快速判读）。

用法（推荐 CLI 形态；uv tool / pipx 隔离装法下 `python -m` 看不到本包）：
    mat summary artifacts/<run_dir>
    mat summary --self-test
等价（仅当当前解释器装着 insta_engine，如 pip --user 装法）：
    python -m insta_engine.run_summary artifacts/<run_dir>

为什么存在：报告 report.html 是自包含 HTML（截图 base64 内联，实测单跑可达 15MB），
agent 整读会撑爆上下文；run.json 才是结构化事实（≈6KB）。本模块把「读大产物」
压缩成「调一次拿小结论」——与 junit_summary（JUnit→CI 摘要）同族，分类标签
复用 report._CATEGORY_LABELS 单一来源，不会两处口径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .report import _CATEGORY_LABELS

MAX_LINES = 40  # 摘要行数上限：超过只显示前 8 个失败步骤，其余折叠
MAX_FAILED_SHOWN = 8


def _load(run_dir: str | Path) -> dict:
    path = Path(run_dir) / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在——run_dir 应是含 run.json 的执行产物目录（如 artifacts/case_xxx）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        # 带路径重包（2026-10-25 评审 P2-1）：裸 JSONDecodeError 没说哪个文件坏了，
        # 调用方（agent/CI）无从行动。收口在这里，`python -m` 直跑同样受益。
        raise ValueError(f"{path} 不是合法 JSON（{exc}）——若被手工改过，重跑该用例生成新产物"
                         ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} 应是对象，实际是 {type(data).__name__}——该文件由 mat run 生成，"
                         "若被手工改过，重跑该用例生成新产物")
    return data


def summarize_run(run_dir: str | Path) -> dict:
    data = _load(run_dir)
    run = data.get("run", {})
    steps = run.get("steps", [])
    failed = [s for s in steps if s.get("status") == "failed"]
    return {
        "case_name": (data.get("case") or {}).get("name") or run.get("case_name", ""),
        "status": run.get("status", "unknown"),
        "step_success_rate": run.get("step_success_rate"),
        "duration_ms": run.get("duration_ms"),
        "device_serial": data.get("device_serial", ""),
        "failed_categories": run.get("failed_categories") or {},
        "failed_steps": failed,
        "total_steps": len(steps),
        "run_dir": str(run_dir),
    }


def _error_first_line(step: dict) -> str:
    error = (step.get("error") or "").strip()
    return error.splitlines()[0] if error else ""


def digest(summary: dict) -> str:
    """→ ≤40 行判读文本。失败步骤排前且必带分类、错误首行与证据路径。"""
    rate = summary.get("step_success_rate")
    rate_text = f"{rate * 100:.0f}%" if isinstance(rate, (int, float)) else "?"
    duration_s = summary.get("duration_ms")
    duration_text = f"{duration_s / 1000:.0f}s" if isinstance(duration_s, (int, float)) else "?"
    lines = [
        f"MAT 运行摘要 — {summary['case_name']}",
        f"状态: {summary['status']} · 步骤成功率 {rate_text}"
        f"（{summary['total_steps']} 步）· 用时 {duration_text}"
        f" · 设备 {summary.get('device_serial') or '?'}",
    ]

    cats: dict[str, int] = summary.get("failed_categories") or {}
    if cats:
        cat_text = " · ".join(
            f"{k}×{v}（{_CATEGORY_LABELS.get(k, k)}）" for k, v in cats.items()
        )
        lines.append(f"失败分类: {cat_text}")

    failed: list[dict] = summary.get("failed_steps") or []
    if not failed:
        lines.append("（无失败步骤——截图证据见 report.html）")
        return "\n".join(lines)

    shown = failed[:MAX_FAILED_SHOWN]
    lines.append(f"失败步骤（{len(failed)}）:")
    for step in shown:
        category = step.get("category") or "unknown"
        label = _CATEGORY_LABELS.get(category, category)
        env_note = "〔环境抖动，不计入产品失败〕" if category == "env-failure" else ""
        lines.append(f"  {step.get('step_id', '?')} [{category}·{label}]{env_note} {step.get('intent', '')}")
        error_line = _error_first_line(step)
        if error_line:
            lines.append(f"      error: {error_line}")
        evidence = " · ".join(p for p in (step.get("screenshot"), step.get("logcat_file")) if p)
        if evidence:
            lines.append(f"      证据: {evidence}")
    if len(failed) > len(shown):
        lines.append(f"  …（其余 {len(failed) - len(shown)} 个失败步骤略，全量见 report.html）")

    lines.append(f"下一步: mat diagnose {summary['run_dir']} → LLM 归因（四分类+证据+建议；置信度<0.6 标 need_human）")
    lines.append("报告（人读，勿整读）: report.html（自包含 HTML，截图内联，实测可达 15MB）")
    return "\n".join(lines)


def self_test() -> int:
    """内置自检：构造样例 run.json 走全链路，断言关键内容都在摘要里。"""
    import tempfile

    sample = {
        "finished_at": "2026-10-03T00:00:00+00:00",
        "device_serial": "TESTSERIAL",
        "case": {"caseId": "case_demo", "name": "自检样例用例"},
        "run": {
            "case_id": "case_demo",
            "case_name": "自检样例用例",
            "status": "failed",
            "step_success_rate": 0.9,
            "duration_ms": 38772,
            "failed_categories": {"assert-fail": 1},
            "steps": [
                {"step_id": "s1", "intent": "启动", "action": "launch", "status": "passed",
                 "duration_ms": 100, "category": None, "error": None},
                {"step_id": "s10", "intent": "断言列表不该出现 undefined", "action": "assert",
                 "status": "failed", "duration_ms": 300, "category": "assert-fail",
                 "error": "AssertionError: 断言失败: 控件不应存在: undefined\n更多行",
                 "screenshot": "screenshots/step_10_s10.png",
                 "logcat_file": "logs/step_10_s10.logcat.txt"},
            ],
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "case_demo"
        (run_dir).mkdir()
        (run_dir / "run.json").write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
        text = digest(summarize_run(run_dir))
    for expected in ("自检样例用例", "failed", "90%", "assert-fail", "s10",
                     "AssertionError", "screenshots/step_10_s10.png", "mat diagnose"):
        assert expected in text, f"自检缺内容: {expected!r}"
    assert len(text.splitlines()) <= MAX_LINES, "摘要超行数上限"
    print(f"✅ self-test 通过（{len(text.splitlines())} 行摘要）")
    return 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="run.json → 运行摘要（≤40 行，AI agent/CI 判读用）")
    ap.add_argument("run_dir", nargs="?", help="执行产物目录（含 run.json，如 artifacts/case_xxx）")
    ap.add_argument("--self-test", action="store_true", help="内置自检")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.run_dir:
        ap.error("需要 run_dir（或 --self-test）")
    try:
        summary = summarize_run(args.run_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(digest(summary))
    if summary["status"] == "passed":
        return 0
    return 1 if summary["status"] in ("failed", "skipped", "cancelled") else 2


if __name__ == "__main__":
    raise SystemExit(main())
