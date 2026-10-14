"""prompt 模板定位与播种（open-core 边界：闭源 prompt 由轻云下发，本地以 *.example 模板兜底）。

定位顺序（`resolve`）：
  ⓪ 工作区 `prompts/.choice.json` 里为该任务**显式选择**的文件   —— 控制台「Prompt 管理」写入
  ① 工作区 `prompts/<name>.prompt`          —— 用户自定义/播种后的生效文件
  ② 工作区 `prompts/<name>.prompt.example`  —— 源码仓库检出场景
  ③ 包内 `insta_engine/prompts/<name>.prompt.example` —— pip/wheel 安装场景

`seed()` 把可用模板复制为工作区 `prompts/<name>.prompt`（不覆盖已存在），由 `mat init` 调用。
背景：wheel 此前不含任何 prompts，PyPI 用户 `mat gen/import` 与视觉兜底全部失败；
包内携带 `.example` + `init` 播种后，安装即用。

模式（`GEN_PROMPT_MODE`）：`local`（默认，本模块生效）或 `server`（轻云代理组装 prompt，
本地不再需要任何 prompt 文件）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 与 prompts/ 目录下的模板一一对应（见 docs/13-产品使用手册.md）
NAMES = ("gen", "convert", "map", "describe", "diagnose", "vision-locate")

PKG_PROMPTS = Path(__file__).parent / "prompts"

CHOICE_FILE = ".choice.json"


def mode() -> str:
    """prompt 组装模式：`local`（默认，本地拼装）或 `server`（轻云代理服务端组装）。"""
    return os.environ.get("GEN_PROMPT_MODE", "local").strip().lower()


def is_server() -> bool:
    return mode() == "server"


def workspace_dir() -> Path:
    """prompt 工作目录。优先 `MAT_WORKSPACE`（控制台 serve() 启动时注入工作区根）——
    控制台任务在进程内跑（cwd=引擎安装目录），不注入的话 `prompts/` 会落在引擎目录下、
    与 CLI 在工作区 cwd 下解析出的目录分裂；CLI 场景无该变量，回落 cwd 相对路径（原行为）。"""
    return Path(os.environ.get("MAT_WORKSPACE") or ".") / "prompts"


def _choice_path(ws: Path) -> Path:
    return ws / CHOICE_FILE


def read_choice(ws: Path | None = None) -> dict:
    """读工作区显式选择 {任务名: 文件名}；缺失/损坏一律 `{}`（⓪ 级不参与）。"""
    try:
        data = json.loads(_choice_path(ws or workspace_dir()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_choice(ws: Path, name: str, filename: str | None) -> None:
    """写入/清除某任务的显式选择（filename=None 即恢复默认解析顺序）。"""
    ws.mkdir(parents=True, exist_ok=True)
    choice = read_choice(ws)
    if filename:
        choice[name] = filename
    else:
        choice.pop(name, None)
    _choice_path(ws).write_text(json.dumps(choice, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def _candidates(name: str, ws: Path):
    chosen = str(read_choice(ws).get(name) or "")
    if chosen and Path(chosen).name == chosen:  # 只认 prompts/ 目录内的裸文件名，防越界
        p = ws / chosen
        if p.exists() and p.is_file():
            yield p
    yield ws / f"{name}.prompt"
    yield ws / f"{name}.prompt.example"
    yield PKG_PROMPTS / f"{name}.prompt.example"


def resolve(name: str, workspace: Path | None = None) -> Path | None:
    """返回可用的 prompt 文件路径；四级均缺失返回 None。"""
    ws = Path(workspace) if workspace else workspace_dir()
    for path in _candidates(name, ws):
        if path.exists():
            return path
    return None


def read(name: str, workspace: Path | None = None) -> str | None:
    path = resolve(name, workspace)
    return path.read_text(encoding="utf-8") if path else None


def describe_resolution(name: str, ws: Path | None = None) -> tuple[str, Path | None]:
    """解析来源标签（doctor/控制台口径展示用）：

    `chosen`（.choice.json 显式选择）/ `workspace`（工作区自定义 <name>.prompt）/
    `workspace-example`（工作区 example）/ `package`（包内模板）/ `missing`。
    """
    p = resolve(name, ws)
    if p is None:
        return "missing", None
    if p.parent == PKG_PROMPTS:
        return "package", p
    if str(read_choice(ws or workspace_dir()).get(name) or "") == p.name:
        return "chosen", p
    return ("workspace" if p.name.endswith(".prompt") and not p.name.endswith(".example")
            else "workspace-example"), p


def candidates_detail(name: str, ws: Path | None = None) -> list[dict]:
    """某任务可选的 prompt 文件清单（控制台「Prompt 管理」用）：工作区全部候选 + 包内模板。

    返回 [{file, source: workspace|package, size, mtime, active}]，active 按 resolve 现算。
    """
    ws = Path(ws) if ws else workspace_dir()
    active_path = resolve(name, ws)
    out: dict[str, dict] = {}
    for p in sorted(ws.glob("*.prompt*")) + sorted(PKG_PROMPTS.glob(f"{name}.prompt*")):
        if not p.is_file() or p.name == CHOICE_FILE:
            continue
        out[p.name] = {"file": p.name,
                       "source": "package" if p.parent == PKG_PROMPTS else "workspace",
                       "size": p.stat().st_size,
                       "mtime": int(p.stat().st_mtime),
                       "active": active_path is not None and p == active_path}
    # 排序：工作区在前、包内在后；同级按修改时间新→旧（自己刚传的排最前）
    return sorted(out.values(),
                  key=lambda d: (d["source"] == "package", -d["mtime"] if d["source"] == "workspace" else 0,
                                 d["file"] if d["source"] == "package" else ""))


def seed(dest_dir: Path | None = None) -> list[str]:
    """播种工作区 prompts/<name>.prompt（不覆盖已存在）；返回新建文件的相对路径列表。"""
    dest = Path(dest_dir) if dest_dir else workspace_dir()
    dest.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for name in NAMES:
        target = dest / f"{name}.prompt"
        if target.exists():
            continue
        source = next(
            (p for p in (dest / f"{name}.prompt.example", PKG_PROMPTS / f"{name}.prompt.example")
             if p.exists()),
            None,
        )
        if source is not None:
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(target.as_posix())
    return created
