"""verify 调用计量：每次判定追加一行 JSON（**只记元数据，不记用例内容**）。

## 为什么要有它

项目把 agent-first 方向写成了**可证伪闸门**：4 周内 `verify` 调用 ≥100 次
且真实 agent 调用者 ≥3 个 ⇒ 加码，达不到就转团队落地服务。但此前引擎里
**没有任何 verify 调用计数**：`verify.py` 只把 verdict 打给 stdout 和产物目录，
到判据那天无法回答「到底跑了多少次」，闸门会变成一句无法裁决的话。

本模块只补**一半**：本机累计了多少次 verify、按日与按判定的分布。
另一半（≥3 个真实调用者）跨用户，纯本地计数天然判不了，必须人工登记或由
服务端统计——这一边界写进 `render()` 的输出里，不许含糊过去。

## 记什么 / 不记什么

只记 `ts` / `schema` / `verdict` / `exit_code` 四项。**不记**用例名、路径、包名、
截图、控件文本、自然语言输入——与「零内容留痕」的既有口径一致，也不触碰
对外隐私承诺。数据只落在 `~/.mat/metrics/verify.jsonl`，**不上传、不联网**。

## 开关与位置

沿用 `MAT_AI_METRICS=0` 这个已有的本地计量总开关（`ai_metrics.py` 同源）：
关掉 AI 计量就一并关掉本计量，测试侧 conftest 已全局设为 0 ⇒ 全量回归不会
往用户真实状态目录写假数据。**不新增任何环境变量**，根目录仍走 `state_home()`
（`MAT_HOME` / `INSTA_HOME` 覆盖与 `~/.insta` 迁移由它统一负责）。

## 口径

一次 `run_verify()` = 一次判定 = 一行。CLI 单条、CLI `--subset` 批量（逐条复用
`run_verify`）、MCP `mat_verify` 单条都走这个薄壳，因此三条路径自动覆盖；
记录挂在与 `_persist_verdict` 同一层，避免 8 个出口逐个漏加。

    mat verify-stats                     # 近 28 天 + 本机累计
    mat verify-stats --since 2026-10-31  # 对齐 4 周闸门的起算日
    mat verify-stats --json              # 给脚本/agent 消费
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .state import state_home

#: 落盘文件名（JSONL，一行一次判定）；轮转时旧文件变 `verify.jsonl.1`。
USAGE_FILE = "verify.jsonl"
#: 4 周闸门里 verify 调用的目标次数。
GATE_CALLS = 100
#: 默认窗口 = 4 周，与闸门周期对齐。
DEFAULT_WINDOW_DAYS = 28
#: 单文件轮转阈值（MB）——只保留一份历史，够分辨「最近 vs 更早」。
MAX_MB = 5


def enabled() -> bool:
    """与 `ai_metrics` 共用本地计量总开关；默认开，`MAT_AI_METRICS=0` 关闭。"""
    return os.environ.get("MAT_AI_METRICS", "1") != "0"


def usage_path() -> Path:
    return state_home() / "metrics" / USAGE_FILE


def _rotate(path: Path) -> None:
    """超过阈值把当前文件挪成 `.1`；轮转失败不该影响判定本身。"""
    try:
        if path.exists() and path.stat().st_size > MAX_MB * 1024 * 1024:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def record(verdict: dict, exit_code: int, *, now: datetime | None = None) -> None:
    """记一次判定。**任何异常都不外抛**——计量坏掉不能把 verify 结论搞挂。

    只取四个标量字段；`verdict` 里其余内容（用例名、证据路径、失败步骤）一律不读。
    """
    if not enabled():
        return
    row = {
        "ts": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "schema": str(verdict.get("schema") or "mat.verify/1"),
        "verdict": str(verdict.get("verdict") or "unknown"),
        "exit_code": int(exit_code),
    }
    path = usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _rows(path: Path) -> list[dict]:
    """读当前文件 + 一份轮转历史；坏行跳过（半行写入/手工编辑都不该让统计崩）。"""
    rows: list[dict] = []
    for source in (path.with_suffix(path.suffix + ".1"), path):
        if not source.exists():
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                datetime.fromisoformat(row.get("ts", ""))
            except (ValueError, TypeError, AttributeError):
                continue
            rows.append(row)
    return rows


def summarize(days: int = DEFAULT_WINDOW_DAYS, *, since: str | None = None,
              now: datetime | None = None, path: Path | None = None) -> dict:
    """汇总本机 verify 调用：全部累计 + 窗口内次数 + 按日/按判定分布。

    `since`（`YYYY-MM-DD`，UTC）优先于 `days`，用于把窗口对齐到闸门起算日；
    `now` 只供测试注入固定时刻。返回值可直接 `json.dumps`。
    """
    now = now or datetime.now(UTC)
    if since:
        try:
            cutoff = datetime.fromisoformat(since).replace(tzinfo=UTC)
        except ValueError:
            cutoff = now - timedelta(days=days)
    else:
        cutoff = now - timedelta(days=days)
    path = path or usage_path()
    rows = _rows(path)
    stamps = sorted(str(r.get("ts", "")) for r in rows)
    in_window = [r for r in rows
                 if datetime.fromisoformat(r["ts"]) >= cutoff]
    by_day: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    for row in in_window:
        day = str(row["ts"])[:10]
        by_day[day] = by_day.get(day, 0) + 1
        v = str(row.get("verdict") or "unknown")
        by_verdict[v] = by_verdict.get(v, 0) + 1
    return {
        "total_all": len(rows),
        "in_window": len(in_window),
        "window_days": days,
        "since": since or cutoff.date().isoformat(),
        "first_ts": stamps[0] if stamps else None,
        "last_ts": stamps[-1] if stamps else None,
        "by_day": dict(sorted(by_day.items())),
        "by_verdict": dict(sorted(by_verdict.items())),
        "gate_calls": GATE_CALLS,
        "gate_met": len(in_window) >= GATE_CALLS,
        "path": str(path),
    }


def render(sm: dict) -> str:
    """人类可读输出。边界必须写在脸上：本机计数 ≠ 闸门全部判据。"""
    lines = [
        "=== verify 调用计量（只读本机，不上传） ===",
        f"本机累计判定 {sm['total_all']} 次 · 窗口内 {sm['in_window']} 次"
        f"（起算 {sm['since']}）",
        f"闸门「verify 调用 ≥{sm['gate_calls']} 次」："
        f"{sm['in_window']}/{sm['gate_calls']} · "
        f"{'达标' if sm['gate_met'] else '未达标'}",
    ]
    if sm["by_verdict"]:
        lines.append("窗口内判定分布：" + " / ".join(
            f"{k} {v}" for k, v in sm["by_verdict"].items()))
    if sm["by_day"]:
        lines.append("按日（UTC，最近 10 天有记录的）：")
        for day, n in list(sm["by_day"].items())[-10:]:
            lines.append(f"  · {day}  {n}")
    lines += [
        "",
        "⚠️ 本计数只覆盖本机这一个实例，不上传、不记用例内容；"
        "「真实 agent 调用者 ≥3 个」跨用户，本地计数判不了 ⇒ 需人工登记或服务端统计。",
        "⚠️ 窗口请对齐 4 周闸门的起算日，必要时用 --since 指定；"
        "本机的部分只是闸门的一半。",
        f"明细：{sm['path']}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    print(render(summarize()))
