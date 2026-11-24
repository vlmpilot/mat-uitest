"""自愈跨次台账（评审 #6「只看单次运行」）。

## 为什么需要它

自愈提案此前只存在**单次运行**的 `run.json` 里，于是同一个控件反复靠兜底命中时，
没人答得上来：这处自愈出现过几次？在哪些 App 版本上出现过？换了 prompt 之后还出现吗？
`artifacts/` 还被 `.gitignore` 忽略，换机/清理就没了。

台账只记**摘要**（一条自愈一行），按 `(case_id, step_id, 原定位)` 聚合后可回答上面三个问题。
不记截图、不记控件树——那些在产物目录里，台账要能进版本库、能 diff。

## 纪律

- **追加写**，不重写历史：聚合时按 `(run_uid, step_id)` 去重。`run_uid` 用**产物目录**——
  同一次运行的目录唯一，重跑同一用例会换目录（带时间戳）。不用时间戳：秒级精度会让
  同秒内的多次真跑被误判成重复（2026-11-24 实测踩到）。
- **失败不影响执行**：台账是观测设施，写不进去（磁盘满 / 权限）不该让一次真机运行失败。
  调用方（`Runner._record_heal_ledger`）负责吞异常。
- `recovered=False` 的行同样记录：它回答"这处自愈**没救回来**过几次"，与"救回来"同样重要。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .state import state_home

FILENAME = "heal_ledger.jsonl"


def ledger_path() -> Path:
    """台账位置（`MAT_HOME` / `INSTA_HOME` 可覆盖，与凭据/配额同一套 home）。"""
    return state_home() / FILENAME


def record_run(run, *, app_package: str = "", app_version: str = "",
               run_uid: str = "") -> int:
    """把一次运行里的自愈追加进台账，返回写入条数。

    `run` 是 `RunResult`（或任何有 `.steps` 且每步有 `.heal` 的对象）。
    """
    rows = []
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    uid = run_uid or uuid.uuid4().hex[:12]
    for step in getattr(run, "steps", []) or []:
        heal = getattr(step, "heal", None)
        if not heal:
            continue
        rows.append({
            "ts": ts,
            "run_uid": uid,
            "case_id": getattr(run, "case_id", "") or "",
            "step_id": getattr(step, "step_id", "") or "",
            "original": heal.get("original", ""),
            "match": heal.get("match", ""),
            "via": heal.get("via", ""),
            "recovered": bool(heal.get("recovered")),
            "app_package": app_package,
            "app_version": app_version,
        })
    if not rows:
        return 0
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def read_rows() -> list[dict]:
    """读全部行；坏行跳过（台账是观测数据，不该因为一行损坏就整体不可读）。"""
    path = ledger_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(rows: list[dict] | None = None) -> list[dict]:
    """按 `(case_id, step_id, original)` 聚合。

    输出按「出现次数」降序：最该去修的是反复靠兜底的那几处。
    """
    rows = read_rows() if rows is None else rows
    groups: dict[tuple, dict] = {}
    seen: set[tuple] = set()
    for row in rows:
        key = (row.get("case_id", ""), row.get("step_id", ""), row.get("original", ""))
        # 同一 (运行, 步骤) 重复 record 只算一次。运行标识优先用 run_uid（产物目录），
        # 老行没有该字段时退回 ts——两种情况都不能重复计数。
        dedup = (*key, row.get("run_uid") or row.get("ts", ""), row.get("step_id", ""))
        if dedup in seen:
            continue
        seen.add(dedup)
        g = groups.setdefault(key, {
            "case_id": key[0], "step_id": key[1], "original": key[2],
            "count": 0, "recovered": 0, "missed": 0,
            "via": set(), "matches": set(), "versions": set(),
            "first_seen": row.get("ts", ""), "last_seen": row.get("ts", ""),
        })
        g["count"] += 1
        if row.get("recovered"):
            g["recovered"] += 1
        else:
            g["missed"] += 1
        if row.get("via"):
            g["via"].add(row["via"])
        if row.get("match"):
            g["matches"].add(row["match"])
        if row.get("app_version"):
            g["versions"].add(row["app_version"])
        ts = row.get("ts", "")
        if ts:
            g["first_seen"] = min(g["first_seen"] or ts, ts)
            g["last_seen"] = max(g["last_seen"] or ts, ts)
    out = []
    for g in groups.values():
        g["via"] = sorted(g["via"])
        g["matches"] = sorted(g["matches"])
        g["versions"] = sorted(g["versions"])
        out.append(g)
    out.sort(key=lambda g: (-g["count"], g["case_id"], g["step_id"]))
    return out
