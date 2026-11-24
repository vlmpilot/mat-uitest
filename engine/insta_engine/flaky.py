"""日常 flaky 统计（评审 #16）。

## 它回答什么

`mat verify-stats` 回答「今天判了多少 pass/fail」；这里回答**「哪条用例反复跑时稳不稳」**。
两者不可互相替代：判定分布看不出"同一用例一半过一半不过"，那正是最该先修的一类。

## 数据来源

`state_home()/history.jsonl` —— `Runner._persist` 每次执行追加一行
（ts / case_id / case_name / status / step_success_rate / duration_ms / failed_categories）。
它已经在跑，不需要额外的埋点。

## 两个口径决定

- **`cancelled` 不算**：中止（超时/取消）既不是"稳"也不是"不稳"，混进来会污染通过率。
- **默认排除测试桩**：单测会往同一份历史里写 `c1`/`n` 这类条目（本机实测 2137 条），
  不排除会把"用例稳定性"变成"测试跑了几遍"的统计。排除数量会打印出来，`--all` 可看全量。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .state import state_home

DEFAULT_MIN_RUNS = 3
DEFAULT_DAYS = 30


def history_path() -> Path:
    return state_home() / "history.jsonl"


def load_rows(path: Path | None = None) -> list[dict]:
    """读执行历史；坏行跳过（历史是观测数据，一行损坏不该让统计不可用）。"""
    path = Path(path) if path else history_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _is_stub(row: dict) -> bool:
    """疑似测试桩（单测写进历史的 c1/n 一类）：id 与名字都极短。"""
    return len(str(row.get("case_id") or "")) <= 2 and len(str(row.get("case_name") or "")) <= 1


def summarize(rows: list[dict], *, min_runs: int = DEFAULT_MIN_RUNS,
              days: int = DEFAULT_DAYS, include_stubs: bool = False,
              exclude: str = "") -> dict:
    """按 (case_id, case_name) 聚合，返回 flaky 清单与汇总计数。

    `exclude`：正则，命中 `case_id` 的记录跳过。用于排掉已知噪声——
    2026-11-24 之前单测会往同一份历史里写（`c1`/`selfheal_noop` 这类），
    仅靠"名字短"识别不出来，所以留一个显式开关。
    """
    import re as _re

    pattern = _re.compile(exclude) if exclude else None
    cutoff = ""
    if days and days > 0:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")

    groups: dict[tuple[str, str], dict] = {}
    excluded_stubs = 0
    for row in rows:
        if not include_stubs and _is_stub(row):
            excluded_stubs += 1
            continue
        if pattern is not None and pattern.search(str(row.get("case_id") or "")):
            continue
        status = row.get("status")
        if status not in ("passed", "failed"):
            continue  # cancelled 既非稳也非不稳
        ts = str(row.get("ts") or "")
        if cutoff and ts and ts < cutoff:
            continue
        key = (str(row.get("case_id") or ""), str(row.get("case_name") or ""))
        g = groups.setdefault(key, {
            "case_id": key[0], "case_name": key[1],
            "runs": 0, "passed": 0, "failed": 0, "recent": [],
        })
        g["runs"] += 1
        g["passed" if status == "passed" else "failed"] += 1
        g["recent"].append((ts, status))

    items: list[dict] = []
    for g in groups.values():
        if g["runs"] < max(1, min_runs):
            continue
        g["pass_rate"] = round(g["passed"] / g["runs"], 4)
        g["flaky"] = 0 < g["passed"] < g["runs"]
        g["recent"].sort(key=lambda pair: pair[0])
        g["recent_status"] = [s for _, s in g["recent"][-5:]]
        g["last_ts"] = g["recent"][-1][0]
        g["last_status"] = g["recent"][-1][1]
        del g["recent"]
        items.append(g)

    # flaky 优先；同为 flaky 时通过率低的在前（最不稳的先看）
    items.sort(key=lambda x: (not x["flaky"], x["pass_rate"], -x["runs"], x["case_id"]))
    flaky = [x for x in items if x["flaky"]]
    return {
        "window_days": days,
        "min_runs": min_runs,
        "total_cases": len(items),
        "flaky_cases": len(flaky),
        "stable_cases": len(items) - len(flaky),
        "excluded_stub_rows": excluded_stubs,
        "exclude": exclude,
        "items": items,
    }


def render(summary: dict) -> str:
    items = summary["items"]
    if not items:
        return (f"没有可统计的用例（近 {summary['window_days']} 天内至少 "
                f"{summary['min_runs']} 次执行；排除测试桩 "
                f"{summary['excluded_stub_rows']} 条）")
    lines = [
        f"=== flaky 统计（近 {summary['window_days']} 天，≥{summary['min_runs']} 次） ===",
        f"flaky {summary['flaky_cases']} 条 / 稳定 {summary['stable_cases']} 条"
        f"（共 {summary['total_cases']} 条；已排除 {summary['excluded_stub_rows']} 条疑似测试桩）",
    ]
    if not summary["flaky_cases"]:
        lines.append("（没有 0 < 通过率 < 1 的用例——当前窗口内不存在时过时不过）")
    mark = {"passed": "✓", "failed": "✗"}
    for it in items[:20]:
        recent = "".join(mark.get(s, "?") for s in it["recent_status"])
        flag = "⚠️ " if it["flaky"] else "   "
        lines.append(
            f"{flag}{it['case_id']:<18}{it['runs']:>4} 次  "
            f"通过 {it['passed']:>3} / 失败 {it['failed']:>3}  "
            f"{it['pass_rate']:>6.1%}  最近 {recent:<7}{it['last_ts'][:16]}")
    if len(items) > 20:
        lines.append(f"  … 另有 {len(items) - 20} 条（--json 看全量）")
    return "\n".join(lines)
