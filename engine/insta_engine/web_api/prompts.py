"""Prompt 文件管理（2026-10-14，发起人：控制台里可选择使用的 prompt 文件）。

控制台对 gen/convert 两个 prompt 槽位提供：候选清单（工作区 prompts/ + 包内模板）、
显式选择（落 `prompts/.choice.json`，解析优先级最高）、新建/上传保存、恢复默认。
选择语义只有一处：`promptstore.resolve`（⓪ 显式选择 > ① 工作区 <name>.prompt >
② 工作区 example > ③ 包内 example），CLI 与控制台同一套。

server 模式下 prompt 由轻云下发，本地选择**不参与生效**——本模块照常工作（选择会被
记住，切回 local 即生效），前端负责提示并禁用误导性操作。

安全口径（同包名/文件名拼接的既有纪律）：文件名一律 basename 化 + 拒绝路径分隔符，
工作区写盘只允许落在 `<root>/prompts/` 内，包内模板只读。
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import promptstore

#: 控制台暴露的槽位（其余 NAMES 任务走同一套解析，将来要放出来只需加一行）
SLOTS: tuple[tuple[str, str], ...] = (("gen", "用例生成"), ("convert", "用例转换"))

#: 保存文件的后缀白名单（解析 glob 是 `*.prompt*`，这里再收紧一步）
_SUFFIXES = (".prompt", ".prompt.example")

MAX_PROMPT_BYTES = 512 * 1024  # 单个 prompt 文件上限（正常模板 ~10KB，留足余量防误传大文件）


def _safe_name(filename: str) -> str | None:
    """裸文件名 + 白名单后缀；不合规返回 None（防目录穿越，见模块 docstring）。"""
    name = str(filename or "").strip()
    if not name or Path(name).name != name or name in (".", "..") \
            or "/" in name or "\\" in name or name.startswith("."):
        return None
    return name if name.endswith(_SUFFIXES) else None


def _slot_family(slot: str, filename: str) -> bool:
    """文件是否属于该槽位的「同族」prompt（发起人 2026-10-14：下拉里不出现其他任务的
    prompt——gen 选了 convert/polish 等别的任务模板只会生成坏结果）。同名与前后缀
    派生都算同族：gen.prompt、gen-v2.prompt、gen_v2.prompt.example、my-gen.prompt。"""
    stem = re.sub(r"\.prompt(\.example)?$", "", filename)
    return stem == slot or stem.startswith((f"{slot}-", f"{slot}_")) \
        or stem.endswith((f"-{slot}", f"_{slot}"))


def _slot(name: str, root: Path) -> dict:
    ws = root / "prompts"
    active = promptstore.resolve(name, ws)
    chosen = str(promptstore.read_choice(ws).get(name) or "")
    # 候选按槽位同族过滤；显式选择过的文件始终保留（哪怕命名不合族规，choice 里指名了）
    candidates = [c for c in promptstore.candidates_detail(name, ws)
                  if c["file"] == chosen or _slot_family(name, c["file"])]
    return {
        "name": name,
        "title": dict(SLOTS).get(name, name),
        "selected": chosen or None,           # 显式选择（可能指向已删除的文件，前端据此提示）
        "active": None if active is None else {
            "file": active.name,
            "source": "package" if active.parent == promptstore.PKG_PROMPTS else "workspace",
            "mtime": int(active.stat().st_mtime),
            "size": active.stat().st_size,
        },
        "candidates": candidates,
    }


def api_prompts(root: Path) -> tuple[int, dict]:
    """GET /api/prompts：模式 + gen/convert 两个槽位的候选与当前生效文件。"""
    return 200, {"mode": promptstore.mode(),
                 "server": promptstore.is_server(),
                 "slots": [_slot(name, root) for name, _ in SLOTS]}


def api_prompts_select(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/prompts/select：{name, file}。file=null/空 = 恢复默认解析顺序。"""
    name = str((body or {}).get("name") or "")
    if name not in dict(SLOTS):
        return 400, {"error": f"不支持的槽位：{name or '(空)'}"}
    raw = str((body or {}).get("file") or "").strip()
    if not raw:  # 恢复默认
        promptstore.write_choice(root / "prompts", name, None)
        return 200, {"ok": True, "slot": _slot(name, root)}
    filename = _safe_name(raw)
    if filename is None:
        return 400, {"error": "文件名不合规：只允许 prompts/ 目录内的裸文件名（.prompt/.prompt.example）"}
    if not (root / "prompts" / filename).exists():
        return 404, {"error": f"工作区 prompts/ 里没有 {filename}"}
    promptstore.write_choice(root / "prompts", name, filename)
    return 200, {"ok": True, "slot": _slot(name, root)}


def api_prompts_save(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/prompts/save：{filename, content} 新建/覆盖工作区 prompt 文件。

    覆盖已存在文件是刻意的（控制台里编辑保存=原地更新）；选择它的动作由 /select 显式做。
    """
    filename = _safe_name(str((body or {}).get("filename") or ""))
    content = str((body or {}).get("content") or "")
    if filename is None:
        return 400, {"error": "文件名不合规：只允许裸文件名，后缀 .prompt 或 .prompt.example"}
    if not content.strip():
        return 400, {"error": "内容为空——想清空请直接删除文件，这里不允许保存空 prompt"}
    data = content.encode("utf-8")
    if len(data) > MAX_PROMPT_BYTES:
        return 400, {"error": f"内容超过 {MAX_PROMPT_BYTES // 1024}KB 上限，请拆分或精简"}
    target = root / "prompts" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(content, encoding="utf-8")
    return 200, {"ok": True, "existed": existed, "slot": None}


def api_prompts_content(root: Path, filename: str) -> tuple[int, dict]:
    """GET /api/prompts/content?file=：读单个 prompt 文件内容（预览/编辑回填）。

    同名时**工作区优先、包内兜底**——与 resolve 的解析语义一致，前端不需要传 scope。
    """
    filename = _safe_name(filename)
    if filename is None:
        return 400, {"error": "文件名不合规"}
    return _read_one(root / "prompts" / filename,
                     promptstore.PKG_PROMPTS / filename, filename)


def _read_one(ws_file: Path, pkg_file: Path, filename: str) -> tuple[int, dict]:
    if ws_file.exists():
        return 200, {"file": filename, "source": "workspace",
                     "content": ws_file.read_text(encoding="utf-8")}
    if pkg_file.exists():
        return 200, {"file": filename, "source": "package",
                     "content": pkg_file.read_text(encoding="utf-8")}
    return 404, {"error": f"找不到 {filename}（工作区与包内模板均无）"}
