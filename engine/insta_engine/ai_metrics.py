"""AI 调用计量：每次请求追加一行 JSON（**只记元数据，不记内容**）。

## 为什么要它

引擎此前**完全不记 token 与耗时**（`ai_client.py` 里 `usage` / `prompt_tokens` 出现 0 次），
于是"单次成本多少、P95 多慢、缓存省了多少"全靠估。成本结论因此只能写成区间。

本模块只做一件事：把每次调用的**元数据**追加到 `~/.mat/metrics/ai.jsonl`，
让成本、延迟、吞吐、缓存收益变成可查的数。

## 为什么不记内容（与「只记次数、不存内容」的对外口径一致）

`nl` / `app_info` / 模型输出都可能含 PII。默认**一个字节都不记**——
否则我们刚验证过的"服务端零留存"会从客户端这边破掉。

需要内容时显式开 `MAT_GEN_TRACE=1`：**只落本地、单独目录、带脱敏标记**，用于将来真要训练时
构造同构样本（题 + 答案）。默认关。

## 开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MAT_AI_METRICS=0` | 开 | 关闭计量（不影响业务）|
| `MAT_GEN_TRACE=1` | 关 | 额外把输入变量与模型原始输出落盘 |
| `MAT_METRICS_MAX_MB` | 5 | 单个 jsonl 超过该大小就轮转成 `.1` |

## 读它

    mat ai-stats              # 近 24 小时汇总
    mat ai-stats --hours 72   # 近 3 天
    python -m insta_engine.ai_metrics   # 等价（未装 CLI 时）
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .state import state_home

#: 计量落盘的文件名（JSONL，一行一次调用）
METRICS_FILE = "ai.jsonl"
#: 内容留痕目录名（仅在 MAT_GEN_TRACE=1 时写入）
TRACE_DIR = "trace"
#: 默认轮转阈值（MB）
DEFAULT_MAX_MB = 5


def metrics_dir() -> Path:
    return state_home() / "metrics"


def metrics_path() -> Path:
    return metrics_dir() / METRICS_FILE


def enabled() -> bool:
    """计量默认开；`MAT_AI_METRICS=0` 关闭。"""
    return os.environ.get("MAT_AI_METRICS", "1") != "0"


def trace_enabled() -> bool:
    """内容留痕默认**关**——开了就落 PII，必须显式同意。"""
    return os.environ.get("MAT_GEN_TRACE") == "1"


def _rotate(path: Path, max_mb: int) -> None:
    """超过阈值就把当前文件挪成 `.1`（只保留一份历史，够定位"最近 vs 更早"）。"""
    try:
        if path.exists() and path.stat().st_size > max_mb * 1024 * 1024:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass  # 轮转失败不该影响业务


def record(*, task: str, model: str = "", ok: bool = True, latency_ms: float | None = None,
           mode: str = "local", prompt_tokens: int | None = None,
           completion_tokens: int | None = None, prompt_chars: int | None = None,
           completion_chars: int | None = None, max_tokens: int | None = None,
           error: str = "", truncated: bool = False,
           scope: str = "call", rounds: int | None = None) -> None:
    """记一次调用。**任何异常都不外抛**——计量坏掉不能把生成搞挂。

    token 数来自端点的 `usage`；端点不返回时（如轻云代理只回 content）退化为字符数，
    调用方可自行按 chars/1.6 估算。`prompt_chars` 只记**长度**，不记内容。

    `scope`（2026-10-25 生成成功率口径分家）：
    - "call"（默认）= 一次**传输层**调用（网络/成本口径）；
    - "task" = 一次**逻辑任务**的最终结局，带 `rounds`（内容修复轮数，1=首穿）。
      「单次生成成功率」的口径 = task 行的首穿率——与「端到端跑通率」分家，勿混引。
    """
    if not enabled():
        return
    row = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "task": task,
        "model": model,
        "mode": mode,
        "ok": bool(ok),
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_chars": prompt_chars,
        "completion_chars": completion_chars,
        "max_tokens": max_tokens,
        "truncated": bool(truncated),
        "error": (error or "")[:160],
        "scope": scope,
        "rounds": rounds,
    }
    path = metrics_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path, _env_int("MAT_METRICS_MAX_MB", DEFAULT_MAX_MB))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def trace(*, task: str, model: str, variables: dict | None, raw_output: str,
          note: str = "") -> Path | None:
    """把一次调用的输入变量与原始输出落盘（**仅 `MAT_GEN_TRACE=1` 时**）。

    用途：将来若要训模型，需要"题 + 答案"同构样本（run.json 只有答案，没有题）。
    落地纪律：
    - 只在本地，不上传；
    - 文件名带时间戳与任务名，便于按批清理；
    - 写 `_meta.json` 标注 `contains_user_data: true`，避免被误当公开数据拷走。
    """
    if not trace_enabled():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    out_dir = metrics_dir() / TRACE_DIR
    path = out_dir / f"{stamp}-{task}.json"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "_meta.json").write_text(json.dumps({
            "contains_user_data": True,
            "note": "本地内容留痕（MAT_GEN_TRACE=1）。含用户输入，禁止上传或入库。",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        path.write_text(json.dumps({
            "ts": stamp, "task": task, "model": model, "note": note,
            "variables": variables or {}, "raw_output": raw_output,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return path
    except OSError:
        return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return ordered[idx]


def summarize(hours: int = 24, path: Path | None = None) -> dict:
    """按任务汇总近 N 小时：次数/失败数/延迟分位/token 合计/截断数。"""
    path = path or metrics_path()
    cutoff = time.time() - hours * 3600
    rows: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            try:
                ts = datetime.fromisoformat(row.get("ts", "")).timestamp()
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                rows.append(row)
    by_task: dict[str, dict] = {}
    # 逻辑任务行(scope=task):首穿率口径——与传输层 call 行分开统计
    first_pass: dict[str, dict] = {}
    for row in rows:
        # 逻辑任务行进首穿口径,不混入传输层统计(成本/延迟只看 call 行)
        if row.get("scope") == "task":
            fp = first_pass.setdefault(row.get("task") or "?",
                                       {"tasks": 0, "ok": 0, "first": 0, "latency_ms": []})
            fp["tasks"] += 1
            # 端到端耗时（2026-10-26 起由 ai_client 直接落盘）。**只统计有值的行**：
            # 老数据（10-26 之前）task 行没有 latency_ms，若按 0 计入会把 p50 拉到 0。
            # 因此另报 `latency_n`，让"样本不足"和"真的很快"可区分。
            if row.get("latency_ms") is not None:
                fp["latency_ms"].append(float(row["latency_ms"]))
            if row.get("ok"):
                fp["ok"] += 1
                if int(row.get("rounds") or 1) == 1:
                    fp["first"] += 1
            continue
        t = by_task.setdefault(row.get("task") or "?", {
            "calls": 0, "failed": 0, "truncated": 0, "latency_ms": [],
            "prompt_tokens": 0, "completion_tokens": 0, "missing_usage": 0,
        })
        t["calls"] += 1
        if not row.get("ok"):
            t["failed"] += 1
        if row.get("truncated"):
            t["truncated"] += 1
        if row.get("latency_ms") is not None:
            t["latency_ms"].append(float(row["latency_ms"]))
        # 只统计**成功**调用缺 usage —— 失败调用本来就没有 token，算进来会误导
        if (row.get("ok") and row.get("prompt_tokens") is None
                and row.get("completion_tokens") is None):
            t["missing_usage"] += 1
        t["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        t["completion_tokens"] += int(row.get("completion_tokens") or 0)
    for t in by_task.values():
        lat = t.pop("latency_ms")
        t["p50_ms"] = int(_percentile(lat, 0.5))
        t["p95_ms"] = int(_percentile(lat, 0.95))
    # total_calls 只数**传输层调用**（2026-10-25）：by_task 已把 scope=task 的行
    # continue 掉，而这里原先直接 len(rows) ⇒ 首行「N 次调用」把逻辑任务行也算成调用
    # （实测 13 = 8 call + 5 task）。两处口径必须同源，否则「调用次数」和下面按任务
    # 列出的 calls 加总对不上，看的人会以为是漏统计。
    total_calls = sum(t["calls"] for t in by_task.values())
    return {"hours": hours, "total_calls": total_calls, "by_task": by_task,
            "task_outcomes": {
                k: {"tasks": v["tasks"], "ok_rate": round(v["ok"] / v["tasks"], 3),
                    "first_pass_rate": round(v["first"] / v["tasks"], 3),
                    # 端到端 P50/P95（含内容级重试）。`latency_n` 是**有效样本数**：
                    # 小于 tasks 说明部分老数据没有耗时字段，此时分位数不可外推。
                    "latency_n": len(v["latency_ms"]),
                    "p50_ms": int(_percentile(v["latency_ms"], 0.5)),
                    "p95_ms": int(_percentile(v["latency_ms"], 0.95))}
                for k, v in first_pass.items() if v["tasks"]}}


def render(summary: dict) -> str:
    lines = [f"近 {summary['hours']} 小时：{summary['total_calls']} 次调用"]
    # 提前返回只看 call 行是错的：`total_calls` 按设计不含 task 行，于是「只有逻辑任务
    # 行、没有传输层行」的窗口会渲染成「无记录」，把已采到的首穿率/端到端耗时一起吞掉
    # （2026-10-26 被新加的单测照出来）。
    if not summary["by_task"] and not summary.get("task_outcomes"):
        return lines[0] + "（无记录——跑一次生成后再看）"
    lines.append(f"{'任务':10s} {'次数':>5s} {'失败':>4s} {'截断':>4s} "
                 f"{'p50':>7s} {'p95':>7s} {'输入tok':>9s} {'输出tok':>9s}")
    for task, s in sorted(summary["by_task"].items()):
        lines.append(f"{task:10s} {s['calls']:>5d} {s['failed']:>4d} {s['truncated']:>4d} "
                     f"{s['p50_ms']:>6d}ms {s['p95_ms']:>6d}ms "
                     f"{s['prompt_tokens']:>9d} {s['completion_tokens']:>9d}")
        if s["missing_usage"]:
            lines.append(f"           └ 其中 {s['missing_usage']} 次成功调用端点未返回 usage，"
                         f"token 只能按字符估算")
    # 逻辑任务结局(口径分家):「单次生成成功率」= 首穿率,与端到端跑通率分开
    for task, fp in sorted(summary.get("task_outcomes", {}).items()):
        lines.append(f"{task} 逻辑任务 {fp['tasks']} 次:成功 {fp['ok_rate']:.0%} "
                     f"首穿 {fp['first_pass_rate']:.0%}(首穿=0 轮内容修复即合法;"
                     f"端到端跑通率另有口径,勿混引)")
        # 端到端耗时（含重试）——这才是「生成一条用例要多久」的可引用口径
        n = fp.get("latency_n", 0)
        if n:
            tail = "" if n == fp["tasks"] else f"（仅 {n}/{fp['tasks']} 条有耗时，老数据无此字段）"
            lines.append(f"{task} 端到端耗时 p50 {fp['p50_ms']}ms / p95 {fp['p95_ms']}ms"
                         f"（n={n}，含内容级重试）{tail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    hours = 24
    if "--hours" in argv:
        try:
            hours = int(argv[argv.index("--hours") + 1])
        except (IndexError, ValueError):
            print("--hours 需要一个整数", file=sys.stderr)
            return 2
    print(render(summarize(hours)))
    print(f"\n明细：{metrics_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
