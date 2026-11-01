"""把 MAT 注册进 AI 客户端的 MCP 配置（`mat mcp install` / `targets` / `show`）。

**为什么需要**：`mat skill install` 早就是"一条命令装到 7 个落点"，而 MCP 那条路一直要
用户（或让助手代劳）自己手写 JSON、还得按客户端分辨配置文件在哪。同一件事两条路一个顺
一个堵 ⇒ 愿意动手的配了 skill，配不动的放弃 MCP——而 MCP 恰恰是 Cursor / Trae 这类
"在对话里直接驱动测试"的入口。

三条纪律，与 `skill.py` 同源，都是踩过的坑：
1. **不猜路径**：只写「官方文档明确 + 本机目录结构对得上」的位置；父目录不存在 ⇒ 拒绝并
   说明原因，**不硬造**（造出来的目录客户端不读，用户却以为配好了，比不配更糟）。
2. **不动别人的键**：只增删 `mcpServers.mat`；写前备份、原子替换，其余内容原样保留。
3. **坏配置不覆盖**：现有文件解析失败 ⇒ 停下并给手工片段，绝不"顺手修好"用户的 JSON。

另外一条经验：**command 写绝对路径**。GUI 客户端（Cursor/Trae 这类）启动子进程时通常
没有登录 shell 的 PATH，写 `"mat"` 会 ENOENT，而它们的报错信息通常只显示"服务器启动失败"。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: 我们在客户端配置里占用的 server 名。固定不变——改名等于让用户多出一份失效配置。
SERVER_NAME = "mat"


@dataclass(frozen=True)
class McpTarget:
    """一个客户端的落点。`note` 会原样打给用户看，`verified` 决定要不要加"请确认"提示。"""

    key: str
    name: str
    path: Path
    note: str = ""
    verified: bool = True


def _app_support() -> Path:
    """各平台的"应用数据目录"。macOS/Linux/Windows 三条，客户端都放在这下面。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def known_targets() -> dict[str, McpTarget]:
    """有据可查的落点。**来源写在 note 里**，方便日后核对与新增。

    刻意不收录的两类：
    - 需要项目级目录的（Trae 的 `.trae/mcp.json`、Cursor 的 `.cursor/mcp.json`、
      VS Code 的 `.vscode/mcp.json`）：它们该跟着项目走，不该由一次全局命令决定；
    - 格式不是"mcpServers + command/args"的（Codex 的 `~/.codex/config.toml` 是 TOML）：
      硬塞会写出客户端读不懂的文件。这类用 `mat mcp show` 拿片段手工加。
    """
    support = _app_support()
    return {
        "cursor": McpTarget(
            "cursor", "Cursor",
            Path.home() / ".cursor" / "mcp.json",
            "用户级（所有项目）；项目级另有 .cursor/mcp.json，同名以项目级优先"),
        "trae": McpTarget(
            "trae", "Trae 国内版",
            support / "Trae CN" / "User" / "mcp.json",
            "用户级；项目级另有 .trae/mcp.json（需在设置里启用「项目级 MCP」）"),
        "claude-desktop": McpTarget(
            "claude-desktop", "Claude Desktop",
            support / "Claude" / "claude_desktop_config.json",
            "改完要完全退出 Claude（不是关窗口）再启动"),
        "workbuddy": McpTarget(
            "workbuddy", "WorkBuddy",
            Path.home() / ".workbuddy" / "mcp.json",
            "写完后在连接器管理里「信任」新条目才会启用"),
    }


def launch_spec(root: Path) -> dict:
    """写进配置里的启动命令。

    优先用可执行的 `mat`；找不到就退回「当前解释器 + -m insta_engine」——两者都写**绝对
    路径**，因为 GUI 客户端不继承登录 shell 的 PATH。
    """
    exe = shutil.which("mat")
    if exe:
        return {"command": exe, "args": ["mcp", "--root", str(root)]}
    return {"command": sys.executable,
            "args": ["-m", "insta_engine", "mcp", "--root", str(root)]}


def snippet(root: Path) -> dict:
    """标准配置片段（`mat mcp show` 打印它，供未收录的客户端手工粘贴）。"""
    return {"mcpServers": {SERVER_NAME: launch_spec(root)}}


def _read_config(path: Path) -> tuple[dict, str | None]:
    """读现有配置。返回 (内容, 错误)。文件不存在 ⇒ ({}, None)。"""
    if not path.exists():
        return {}, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"读不了 {path}：{exc}"
    if not raw.strip():
        return {}, None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {}, (f"{path} 不是合法 JSON（{exc}）——已停止，未做任何修改。\n"
                    "  请先修好它，或用 `mat mcp show` 拿片段手工加。")
    if not isinstance(data, dict):
        return {}, f"{path} 的顶层不是对象——已停止，未做任何修改。"
    return data, None


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, bak)
    return bak


def _write_atomic(path: Path, data: dict) -> None:
    """先写同目录临时文件再替换：中途失败不会留下半截配置。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".mat-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def install(target: McpTarget, root: Path, *, dry_run: bool = False) -> dict:
    """把 `mat` 写进目标客户端的配置。

    返回 {ok, path, changed, backup?, error?, entry}。**不抛异常**——调用方要能就地把
    失败原因打给用户。
    """
    entry = launch_spec(root)
    path = target.path
    if not path.parent.exists():
        return {"ok": False, "path": path, "changed": False, "entry": entry,
                "error": (f"没找到 {target.name} 的配置目录 {path.parent}——"
                          "看起来这个客户端没装，或装在别处。\n"
                          "  · 确认已安装并至少启动过一次（配置目录通常在首次启动时创建）；\n"
                          "  · 或用 --config <你的配置文件> 显式指定。")}

    data, err = _read_config(path)
    if err:
        return {"ok": False, "path": path, "changed": False, "entry": entry, "error": err}

    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return {"ok": False, "path": path, "changed": False, "entry": entry,
                "error": f"{path} 里的 mcpServers 不是对象——已停止，未做任何修改。"}
    servers = dict(servers or {})
    if servers.get(SERVER_NAME) == entry:
        return {"ok": True, "path": path, "changed": False, "entry": entry,
                "note": "已经是这个配置了，没有改动"}

    servers[SERVER_NAME] = entry
    data["mcpServers"] = servers

    if dry_run:
        return {"ok": True, "path": path, "changed": True, "entry": entry,
                "dry_run": True, "preview": json.dumps(data, ensure_ascii=False, indent=2)}

    bak = _backup(path)
    try:
        _write_atomic(path, data)
    except OSError as exc:
        return {"ok": False, "path": path, "changed": False, "entry": entry,
                "error": f"写不进 {path}：{exc}（原文件未动，备份在 {bak}）"}
    return {"ok": True, "path": path, "changed": True, "entry": entry, "backup": bak}


def uninstall(target: McpTarget, *, dry_run: bool = False) -> dict:
    """只摘掉 `mcpServers.mat`，其余内容原样保留。"""
    path = target.path
    data, err = _read_config(path)
    if err:
        return {"ok": False, "path": path, "changed": False, "error": err}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return {"ok": True, "path": path, "changed": False, "note": "本来就没有这一项"}

    servers = dict(servers)
    servers.pop(SERVER_NAME, None)
    if servers:
        data["mcpServers"] = servers
    else:
        data.pop("mcpServers", None)

    if dry_run:
        return {"ok": True, "path": path, "changed": True, "dry_run": True,
                "preview": json.dumps(data, ensure_ascii=False, indent=2)}
    bak = _backup(path)
    try:
        _write_atomic(path, data)
    except OSError as exc:
        return {"ok": False, "path": path, "changed": False,
                "error": f"写不进 {path}：{exc}（原文件未动，备份在 {bak}）"}
    return {"ok": True, "path": path, "changed": True, "backup": bak}


def detected_keys() -> list[str]:
    """本机已经存在的客户端（配置目录在），**按 known_targets 的顺序**返回。

    顺序稳定是有意的：`install` 未指定 `--target` 时要按这个顺序给建议命令，
    set 的遍历顺序在不同进程里不一样，用户看到的提示会飘。
    """
    return [k for k, t in known_targets().items() if t.path.parent.exists()]


def mcp_dependency_ready() -> bool:
    """当前解释器能不能 import mcp。

    `mat mcp install` 本身不需要这个依赖（只写 JSON），但**客户端要启动的那个进程**需要。
    不检查的话会出现最难解释的一种失败：命令说"✅ 已写入"，重启客户端却起不来，而客户端
    的报错通常只有一句"服务器启动失败"。
    """
    try:
        import mcp  # noqa: F401
    except Exception:  # noqa: BLE001 —— 缺依赖/版本不兼容都按"不可用"处理
        return False
    return True


def resolve_target(requested: str | None,
                   config: str | None = None) -> tuple[McpTarget | None, str | None]:
    """决定往哪个客户端写。**没给 `--target` 时按本机检测**——大多数人不需要先知道
    自己用的客户端在工具里叫什么键。

    - 只命中一个 ⇒ 直接用它（零思考路径）；
    - 命中多个 ⇒ 报错并给出**可复制的**命令。刻意不交互提问：这条命令会被脚本和
      agent 调用，它们没法回答 y/n；
    - 一个都没有 ⇒ 报错 + 列出全部键 + `--config` 兜底（不猜"你可能想装哪个"）。
    """
    if config:
        return McpTarget("custom", "自定义配置", Path(config).expanduser()), None
    known = known_targets()
    if requested:
        tgt = known.get(requested)
        if tgt is None:
            return None, f"未知 --target：{requested}（用 `mat mcp targets` 看可选项）"
        return tgt, None

    found = detected_keys()
    if len(found) == 1:
        return known[found[0]], None
    if not found:
        return None, ("没检测到已知客户端（这些配置目录都不存在）。\n"
                      f"  · 已收录：{' / '.join(known)}\n"
                      "  · 先把客户端装好并至少启动一次（配置目录通常在首次启动时创建），或\n"
                      "  · 用 `--config <配置文件路径>` 直接指定。")
    lines = "\n".join(f"    mat mcp install --target {k}" for k in found)
    return None, f"检测到 {len(found)} 个客户端，指定要写哪一个：\n{lines}"
