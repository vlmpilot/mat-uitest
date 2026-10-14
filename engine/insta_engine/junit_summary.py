"""JUnit XML → CI 摘要（GitHub Actions PR 评论 / Markdown 报告用）。

用法（Action 内，pip install mat-uitest 后）：
    python -m insta_engine.junit_summary artifacts/mat-junit.xml --markdown > summary.md
    python -m insta_engine.junit_summary artifacts/mat-junit.xml            # JSON 摘要
与 render_junit_xml 同源：分类标签、失败消息都来自同一份数据，不会两处口径。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .report import _CATEGORY_LABELS

ATTR_LABELS = {
    "app-bug": "真 bug", "ui-change": "UI 改版", "env-flake": "环境抖动",
    "precondition": "前置缺失",
}


def summarize(junit_path: str | Path) -> dict:
    root = ET.parse(junit_path).getroot()
    total = int(root.get("tests", 0))
    cases: list[dict] = []
    for tc in root.iter("testcase"):
        failure = tc.find("failure")
        if failure is None:
            # <error>（框架级异常）此前既不被计为失败、又被动算进 failed 差值——
            # 2026-10-12 评审：failed 改为自洽计数，error 与 failure 同口径。
            failure = tc.find("error")
        case = {
            "name": tc.get("name", ""),
            "classname": tc.get("classname", ""),
            "status": "passed" if failure is None else "failed",
            "time": float(tc.get("time", 0) or 0),
        }
        if failure is not None:
            category = failure.get("type", "unknown")
            case["category"] = category
            case["category_label"] = ATTR_LABELS.get(category, _CATEGORY_LABELS.get(category, category))
            case["message"] = (failure.get("message", "") or failure.text or "")[:300]
        cases.append(case)
    passed = sum(1 for c in cases if c["status"] == "passed")
    failed = sum(1 for c in cases if c["status"] == "failed")
    return {"total": total, "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "cases": cases}


def markdown(summary: dict, title: str = "MAT UI 测试") -> str:
    """→ PR 评论 Markdown。错误成本不对称：失败的行永远排前面且必带分类与建议。"""
    rate = f"{summary['pass_rate'] * 100:.0f}%"
    lines = [f"## {title} · {summary['passed']}/{summary['total']} 通过（{rate}）", ""]
    failed = [c for c in summary["cases"] if c["status"] == "failed"]
    if not failed:
        lines.append("✅ 全部通过——每步截图证据见报告 artifact。")
        return "\n".join(lines)
    lines.append("### 失败明细")
    for c in failed:
        label = c.get("category_label", c.get("category", "unknown"))
        env_note = "（环境抖动，不计入产品失败）" if c.get("category") == "env-failure" else ""
        lines.append(f"- **`{c['name']}`** · {label}{env_note}")
        if c.get("message"):
            lines.append(f"  - {c['message']}")
    lines.append("")
    lines.append("> 每步截图证据见上方 artifact 中的自包含 HTML 报告；"
                 "分类由规则引擎给出，`mat diagnose` 可生成带证据的 LLM 归因。")
    return "\n".join(lines)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="JUnit XML → CI 摘要")
    ap.add_argument("junit", help="JUnit XML 路径")
    ap.add_argument("--markdown", action="store_true", help="输出 Markdown（缺省 JSON）")
    ap.add_argument("--title", default="MAT UI 测试")
    args = ap.parse_args()
    summary = summarize(args.junit)
    if args.markdown:
        print(markdown(summary, title=args.title))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
