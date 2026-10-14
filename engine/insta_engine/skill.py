"""mobileapp-testing skill：随包分发 + 一键装进 AI 编码助手。

skill 是**面向 AI 编码助手的过程知识**（`SKILL.md`：何时跑用例、何时探索、怎么读结果），
不是可执行代码，也不是 MAT 自身运行所需的东西。

**为什么必须随包分发**（2026-10-11 修）：此前它只存在于仓库根目录、靠公开仓分发 ⇒
只 `pip install mat-uitest` 的用户机器上**根本没有这个文件**——用户说"帮我把 skill 装上"，
AI 助手无料可装，只能凭空编一份 SKILL.md 出来（那是幻觉产物，还会伪装成"装好了"）。
现已移入包内 `insta_engine/skills/mobileapp-testing/`，由本模块给出落点与安装动作。
"""

from __future__ import annotations

import shutil
from pathlib import Path

SKILL_NAME = "mobileapp-testing"

# AI 编码助手的**用户级 skill 目录**，键即 `mat skill install --target` 的取值。
# 只列约定明确的两个：Claude Code / CodeBuddy 系走 ~/.claude/skills，WorkBuddy AI 走
# ~/.workbuddy/skills。其余（Cursor 的项目级规则等）用 `--dir` 显式指定——
# **不猜路径**：猜错会把文件写到用户根本不会去读的地方，比不装更糟。
KNOWN_TARGETS = {
    "claude": "~/.claude/skills",
    "workbuddy": "~/.workbuddy/skills",
}


def bundled_skill_dir() -> Path:
    """包内 skill 目录（随 wheel 分发；缺了说明安装包不完整）。"""
    return Path(__file__).parent / "skills" / SKILL_NAME


def bundled_files() -> list[Path]:
    """包内 skill 的文件清单（排序保证输出与测试可复现）。"""
    directory = bundled_skill_dir()
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file())


def resolve_target(target: str | None = None, directory: str | None = None) -> Path:
    """解析安装根目录：`--dir` 优先，其次 `--target` 的已知落点。"""
    if directory:
        return Path(directory).expanduser()
    key = target or "claude"
    if key not in KNOWN_TARGETS:
        raise ValueError(
            f"未知 --target：{key}。可选：{'、'.join(sorted(KNOWN_TARGETS))}；"
            "或用 --dir <路径> 指定")
    return Path(KNOWN_TARGETS[key]).expanduser()


def install(target_root: Path, force: bool = False) -> dict:
    """把包内 skill 复制到 `<target_root>/<SKILL_NAME>/`。

    status 三种取值：
      installed   —— 已写入（`overwritten=True` 表示覆盖了旧版）
      up-to-date  —— 目标已与包内逐字节一致，无需动作
      differs     —— 目标已存在但内容不同 ⇒ **不覆盖**，需用户显式 `--force`

    为什么默认不覆盖：`SKILL.md` 是**用户可能改过**的文件（例如把自己团队的约定补进去），
    静默覆盖会吃掉人家的修改。与 `mat heal apply`「回写不覆盖原用例」同一哲学。
    """
    files = bundled_files()
    if not files:
        raise FileNotFoundError(
            f"包内找不到 skill（{bundled_skill_dir()}）——安装包不完整。"
            "请重装：pip install --force-reinstall mat-uitest")
    dest = target_root / SKILL_NAME
    differing = [
        p.name for p in files
        if not (dest / p.name).is_file() or (dest / p.name).read_bytes() != p.read_bytes()
    ]
    names = [p.name for p in files]
    if not differing:
        return {"status": "up-to-date", "target": dest, "files": names, "differing": []}
    existed = dest.is_dir()
    if existed and not force:
        return {"status": "differs", "target": dest, "files": names, "differing": differing}
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copyfile(path, dest / path.name)
    return {"status": "installed", "target": dest, "files": names,
            "differing": differing, "overwritten": existed}
