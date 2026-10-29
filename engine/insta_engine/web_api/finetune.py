"""/api/finetune GET/POST(微调数据域)——控制台「微调数据」卡的数据面。

GET:各 App 可回溯对数 + 门槛进度(nl 回溯从 2026-10-28 起,存量 0 是如实数字);
POST export:抽取 → dataset/(工作区内),返回统计——训练本体不上控制台
(1-2 小时长任务 + mlx 环境,CLI 才是它的界面)。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..finetune import MIN_PAIRS, collect_pairs, export_dataset


def api_finetune_stats(root: Path) -> tuple[int, dict]:
    r = collect_pairs(root)
    per_app = r["per_app"]
    return 200, {
        "pairs": len(r["pairs"]),
        "no_nl": r["no_nl"],
        "per_app": per_app,
        "min_pairs": MIN_PAIRS,
        "enough": len(r["pairs"]) >= MIN_PAIRS,
        "note": ("数据积累自 2026-10-28 起（生成时自动记录原始需求）——"
                 "此前的历史执行没有该记录"),
    }


def api_finetune_export(root: Path, body: dict) -> tuple[int, dict]:
    app = str((body or {}).get("app") or "").strip() or None
    stats = export_dataset(root, app, Path(root) / "dataset")
    return 200, {"ok": True, **stats}
