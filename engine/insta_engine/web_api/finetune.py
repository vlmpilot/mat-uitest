"""/api/finetune GET/POST(微调数据域)——控制台「微调数据」卡的数据面。

GET:可训练对数/留出规模 + 门槛进度(nl 回溯从 2026-10-28 起,存量 0 是如实数字);
POST export:抽取 → dataset/(工作区内),返回统计——训练本体不上控制台
(1-2 小时长任务 + mlx 环境,CLI 才是它的界面)。

门槛按**可训练对**(train + valid)判,不含留出集:留出集训练时不可见,不能算进
"够 200 对"的进度里,否则卡片显示够训而 `mat finetune train` 仍拒绝。
"""

from __future__ import annotations

from pathlib import Path

from ..finetune import MIN_PAIRS, collect_pairs, export_dataset, split_pairs


def api_finetune_stats(root: Path) -> tuple[int, dict]:
    r = collect_pairs(root)
    split = split_pairs(r["pairs"])
    trainable = len(split["train"]) + len(split["valid"])
    return 200, {
        "pairs": len(r["pairs"]),
        "train_pairs": trainable,
        "holdout_pairs": len(split["holdout"]),
        "no_nl": r["no_nl"],
        "per_app": r["per_app"],
        "min_pairs": MIN_PAIRS,
        "enough": trainable >= MIN_PAIRS,
        "note": ("数据积累自 2026-10-28 起（生成时自动记录原始需求）——"
                 "此前的历史执行没有该记录"),
    }


def api_finetune_export(root: Path, body: dict) -> tuple[int, dict]:
    app = str((body or {}).get("app") or "").strip() or None
    stats = export_dataset(root, app, Path(root) / "dataset")
    return 200, {"ok": True, **stats}
