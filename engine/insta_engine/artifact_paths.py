"""运行产物目录的**唯一**命名入口（2026-11-02 统一）。

## 为什么需要

此前同一件事有三套目录模型，`stale` 判定与报告链接各写一套特判：

| 路径 | 来源 | 问题 |
|---|---|---|
| `artifacts/<stem>` | CLI `mat run` / 控制台单条 | **固定目录**：重跑直接覆盖 run.json |
| `artifacts/verify/<stem>-<stamp>` | `mat verify` | 唯一，但与上面不同层 |
| `artifacts/<suite>/<NN>_<stem>` | 套件 | 本来就唯一 |

固定目录是「状态 failed + 验证结论 pass」自相矛盾的根源：`verify.json` 与
`run.json` 是两个时刻写的，后来的 `mat run` 只覆盖一个，另一个留着。

## 统一后的模型

- **单条用例（CLI / 控制台 / verify）**：`artifacts/<stem>-<yyyymmdd-HHMMSS>[-n]`
  —— 每次运行独立，不再覆盖；
- **套件**：`artifacts/<suite>/<NN>_<stem>`（不变）；
- **数据驱动**：`artifacts/<stem>-ds/<group>`（不变）；
- 旧固定目录 `artifacts/<stem>` **只读兼容**（历史产物照常展示），不再写入。

同一秒内同一用例跑两次会追加 `-2`/`-3`，避免撞名。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def new_run_dir(root: Path | str, stem: str, *, subdir: str | None = None) -> Path:
    """为一次运行分配唯一目录并创建；返回路径（保留调用方给的 root 形态）。"""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = Path(root) / "artifacts"
    if subdir:
        base = base / subdir
    candidate = base / f"{stem}-{stamp}"
    n = 1
    while candidate.exists():
        n += 1
        candidate = base / f"{stem}-{stamp}-{n}"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate
