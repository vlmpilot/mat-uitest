#!/usr/bin/env python3
"""评测台账：把一次跑测的**结果摘要**落到版本库里（`benchmarks/results/ledger.jsonl`）。

## 为什么需要它

`artifacts/` 被 `.gitignore` 忽略 ⇒ **换机/清目录就没了**，而 `docs/` 里只写汇总数字。
后果是：下次改 prompt / 换模型想对比"检出率变没变"时，只能凭记忆翻聊天记录。

台账只存**摘要**（每臂一行：用例数 / 通过 / 失败 / 失败清单 / 设备 / 引擎版本 / 产物路径），
不存截图与 UI 树——所以它小、能入库、能 diff。

## 用法

    # 记录一个臂（arm 名默认取产物目录名）
    python scripts/bench_ledger.py record \
        --benchmark benchmarks/tasks/ios-defect-probes.json \
        --artifacts artifacts/bench/ios-base

    # 一次记多个
    python scripts/bench_ledger.py record --benchmark benchmarks/tasks/m1-defect-probes.json \
        --artifacts artifacts/bench/android-defect-r1 artifacts/bench/android-defect-free

    # 看台账 / 校验台账与磁盘是否还对得上
    python scripts/bench_ledger.py list
    python scripts/bench_ledger.py verify

## 纪律

- **数字只从 `run.json` 读**，不接受命令行传入（否则台账会变成"我说"）。
- `verify` 发现产物已不在时**不删记录**，只标 `artifacts_missing` —— 历史成绩不该因为
  清磁盘而消失，但也不能假装证据还在。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "benchmarks" / "results" / "ledger.jsonl"


def _engine_version() -> str:
    """引擎版本（单源在 `engine/insta_engine/__init__.py`）。取不到返回 `unknown`。"""
    try:
        text = (ROOT / "engine" / "insta_engine" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def _first_error(run: dict) -> str:
    """失败步的第一条错误（截断）——台账里有一句就够定位，细节看产物。"""
    for step in run.get("steps") or []:
        if step.get("status") != "passed" and step.get("error"):
            return str(step["error"]).splitlines()[0][:160]
    return ""


def summarize_arm(artifacts: Path) -> dict:
    """统计一个产物目录（内含若干 `<序号>_<caseId>/run.json`）。"""
    runs = []
    for p in sorted(artifacts.glob("*/run.json")):
        runs.append(json.loads(p.read_text(encoding="utf-8")))
    if not runs:
        raise SystemExit(f"没有 run.json：{artifacts}")

    first = runs[0]
    passed = [r for r in runs if r["run"]["status"] == "passed"]
    failed = [r for r in runs if r["run"]["status"] != "passed"]
    steps_total = sum(len(r["run"]["steps"]) for r in runs)
    steps_ok = sum(1 for r in runs for s in r["run"]["steps"] if s.get("status") == "passed")
    dev = first.get("device") or {}
    return {
        "cases": len(runs),
        "passed": len(passed),
        "failed": len(failed),
        "steps_total": steps_total,
        "steps_passed": steps_ok,
        "duration_s": round(sum(r["run"]["duration_ms"] for r in runs) / 1000, 1),
        "platform": first.get("platform", ""),
        "device": f"{dev.get('model', '')} · {dev.get('os_version', '')}".strip(" ·"),
        "serial": first.get("device_serial", ""),
        "failed_cases": [
            {"id": r["run"]["case_id"], "name": r["run"]["case_name"],
             "error": _first_error(r["run"])}
            for r in failed
        ],
    }


def _read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _write_ledger(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    LEDGER.write_text(body + "\n", encoding="utf-8")


def cmd_record(args) -> int:
    bench = Path(args.benchmark)
    if not bench.exists():
        raise SystemExit(f"benchmark 不存在：{bench}")
    bench_name = bench.stem
    rows = _read_ledger()
    added = 0
    for raw in args.artifacts:
        art = Path(raw)
        if not art.exists():
            raise SystemExit(f"产物目录不存在：{art}")
        arm = args.arm or art.name
        rel = str(art.resolve().relative_to(ROOT)) if art.resolve().is_relative_to(ROOT) else str(art)
        key = (bench_name, arm, rel)
        if any((r["benchmark"], r["arm"], r["artifacts"]) == key for r in rows) and not args.force:
            print(f"  跳过（已记录）：{arm}")
            continue
        s = summarize_arm(art)
        rows.append({
            "date": args.date or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d"),
            "benchmark": bench_name,
            "arm": arm,
            "note": args.note,
            "engine": _engine_version(),
            "artifacts": rel,
            **s,
        })
        added += 1
        print(f"  ✅ {arm}: {s['passed']}/{s['cases']} 通过（步骤 {s['steps_passed']}/{s['steps_total']}，"
              f"{s['duration_s']}s）")
    _write_ledger(rows)
    print(f"台账：{LEDGER.relative_to(ROOT)}（新增 {added} 条，共 {len(rows)} 条）")
    return 0


def cmd_list(args) -> int:
    rows = _read_ledger()
    if not rows:
        print("台账为空")
        return 0
    print(f"{'日期':11s} {'基准':24s} {'臂':32s} {'平台':7s} {'通过':>9s} {'耗时':>8s}")
    for r in rows:
        print(f"{r['date']:11s} {r['benchmark'][:24]:24s} {r['arm'][:32]:32s} "
              f"{r['platform'][:7]:7s} {r['passed']:>4d}/{r['cases']:<4d} {r['duration_s']:>7.1f}s")
    return 0


def cmd_verify(args) -> int:
    """校验台账与磁盘是否还对得上（产物被清理时不删记录，只报告）。"""
    rows = _read_ledger()
    bad = 0
    for r in rows:
        art = ROOT / r["artifacts"]
        if not art.exists():
            print(f"  ⚠️ 产物已不在：{r['arm']}（记录保留，{r['date']}）")
            bad += 1
            continue
        now = summarize_arm(art)
        if (now["cases"], now["passed"]) != (r["cases"], r["passed"]):
            print(f"  ❌ 与磁盘不符：{r['arm']} 台账 {r['passed']}/{r['cases']} "
                  f"vs 磁盘 {now['passed']}/{now['cases']}")
            bad += 1
    print(f"校验完成：{len(rows)} 条，异常 {bad} 条")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="评测台账（结果摘要入库，产物留在 artifacts/）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="记录一个或多个臂")
    rec.add_argument("--benchmark", required=True, help="探针集 JSON（决定 benchmark 名）")
    rec.add_argument("--artifacts", required=True, nargs="+", help="产物目录（可多个）")
    rec.add_argument("--arm", help="臂名；不给则取产物目录名")
    rec.add_argument("--date", help="覆盖日期（补录历史时用）")
    rec.add_argument("--note", default="", help="一句话说明（改动/环境）")
    rec.add_argument("--force", action="store_true", help="同键重复记录也写入")
    rec.set_defaults(func=cmd_record)

    sub.add_parser("list", help="打印台账").set_defaults(func=cmd_list)
    sub.add_parser("verify", help="校验台账与磁盘一致").set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
