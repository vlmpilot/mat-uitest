"""写操作审计的读取端（团队协作 P1，发起人 2026-10-12）。

写入在 web_server.ConsoleHandler._audit（HTTP 层统一记——所有 POST 的必经点，
业务 api_* 零改动）。本模块只负责读：配置台「设置 → 操作记录」展示最近 N 条，
回答「谁改的、谁下发的」。

注意：任务下发（run/gen/explore...）同时在 job meta 里记了 operator（任务条/日志
展示用）；本审计是**全量写操作**的补充——编辑/删除用例、保存套件、导入等
job 之外的动作只在这里有记录。
"""

from __future__ import annotations

import json
from pathlib import Path


def api_audit(root: Path, limit: int = 50) -> tuple[int, dict]:
    """返回最近 limit 条审计（倒序，最新在前）。文件不存在=无记录，返回空而非报错。"""
    try:
        cap = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        cap = 50
    audit = root / "artifacts" / "audit.jsonl"
    if not audit.exists():
        return 200, {"items": [], "total": 0}
    try:
        lines = audit.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 200, {"items": [], "total": 0}
    items = []
    for line in lines[-cap:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 半截行（宕机尾部）跳过，不炸整个列表
    items.reverse()
    return 200, {"items": items, "total": len(lines)}
