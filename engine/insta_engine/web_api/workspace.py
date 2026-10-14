"""工作区域：清单读取（case_meta/runs/imports/explores/indexes）+
切换/迁移/目录浏览（发起人需求）。

- GET /api/fs?path=        服务端目录浏览器（供"本地文件夹选择窗"；**分享模式下禁用**）
- POST /api/workspace/switch  切换工作区 + 可选迁移（不迁移 / 用例+图谱 / 完整）

运行态设计：工作区根目录挂在 **server 实例**（`srv.ws_root`）上而非 handler 类属性——
同一进程可在线切换；后台运行中的旧任务继续写在旧工作区直到结束（不中断）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

#: 用例展示编号分配锁（case_meta 可能被并发 state 请求调用，编号不能重发）
_CASE_NO_LOCK = threading.Lock()

#: 迁移档位 → 要搬的子目录（相对工作区根）。
_MIGRATE_DIRS: dict[str, list[str]] = {
    "none": [],
    "graph": ["cases", "artifacts/page_index"],
    "full": ["cases", "artifacts"],
}


def _local_only(host: str) -> tuple[int, dict] | None:
    """目录浏览/切换是本机能力：`--share`（局域网暴露）下必须禁用，防信息泄露。"""
    if host not in ("127.0.0.1", "localhost"):
        return 403, {"error": "分享模式下禁用工作区浏览/切换（本机直连时可用）"}
    return None


def api_fs_browse(host: str, path: str | None, ext: str | None = None) -> tuple[int, dict]:
    """GET /api/fs?path=&ext=.apk：列出目录下的子目录与匹配后缀的文件。

    ext（逗号分隔，如 ".apk" 或 ".app"）传入时同时返回 files——安装选择器用；
    不传则只列目录（工作区切换器行为不变）。隐藏条目跳过。"""
    gate = _local_only(host)
    if gate:
        return gate
    target = Path(path or Path.home()).expanduser()
    if not target.is_dir():
        return 400, {"error": f"目录不存在: {target}"}
    exts = tuple(e.strip().lower() for e in ext.split(",")) if ext else None
    dirs, files = [], []
    try:
        for child in sorted(target.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                dirs.append(child.name)
            elif exts and child.suffix.lower() in exts:
                files.append(child.name)
    except OSError as exc:
        return 400, {"error": f"无法读取目录: {exc}"}
    parent = str(target.parent) if target.parent != target else None
    return 200, {"path": str(target), "parent": parent, "dirs": dirs, "files": files}


def open_path_in_file_manager(path: str) -> None:
    """用系统文件管理器/默认程序打开路径（目录→文件管理器；文件→默认编辑器）。

    doctor 一键修复的「打开配置文件」与工作区「打开」按钮共用；调用方负责本机 gate。"""
    target = Path(path).expanduser()
    if sys.platform.startswith("win"):
        cmd = ["explorer", str(target)]
    elif sys.platform == "darwin":
        cmd = ["open", str(target)]
    else:
        cmd = ["xdg-open", str(target)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def api_fs_open(host: str, path: str) -> tuple[int, dict]:
    """POST /api/fs/open：在本机文件管理器（Finder / 资源管理器 / xdg-open）打开目录。

    概览页工作区模块的「打开」按钮用——与浏览/切换同一条本机红线：--share 下禁用；
    只允许目录（文件不给开），列表参数 Popen 不经 shell。"""
    gate = _local_only(host)
    if gate:
        return gate
    target = Path(path or "").expanduser()
    if not target.is_dir():
        return 400, {"error": f"目录不存在: {target}"}
    open_path_in_file_manager(str(target))
    return 200, {"ok": True, "path": str(target)}


def api_workspace_switch(server, old_root: Path, host: str, body: dict) -> tuple[int, dict]:
    """POST /api/workspace/switch：切换工作区；migrate=none|graph|full（拷贝，不删旧）。"""
    gate = _local_only(host)
    if gate:
        return gate
    raw = str(body.get("path", "")).strip()
    if not raw:
        return 400, {"error": "需要 path"}
    new_ws = Path(raw).expanduser().resolve()
    if new_ws == Path(old_root).resolve():
        return 400, {"error": "新工作区与当前相同"}
    migrate = str(body.get("migrate", "none"))
    if migrate not in _MIGRATE_DIRS:
        return 400, {"error": f"未知迁移档位 {migrate!r}（none/graph/full）"}

    new_ws.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for rel in _MIGRATE_DIRS[migrate]:
        src = Path(old_root) / rel
        if not src.exists():
            continue
        dst = new_ws / rel
        shutil.copytree(src, dst, dirs_exist_ok=True)
        moved.append(rel)

    # 切换挂在 server 实例上：后续请求（同一 server）即用新工作区；
    # 运行中的旧任务持有旧 root 引用，跑完为止（不中断）。
    server.ws_root = new_ws
    return 200, {"ok": True, "root": str(new_ws), "migrated": moved,
                 "hint": "已切换。运行中的旧任务会继续写在原工作区直到结束；"
                         "重启 console 后默认回到启动目录（可用 --root 固定）。"}


# -- 清单读取（自 Phase 0 搬入；state.py 与报告/执行视图的数据源） --------------------

def case_meta(root: Path) -> dict:
    """用例文件 → {package, title} 映射（配置台按 App 分组/联动的数据源）。

    「no」= 展示用例编号 #N（发起人 2026-10-12：caseId 随机字母难沟通且大量重复）。
    每个用例文件首次出现时分配递增数字，持久化在 artifacts/.case_no.json——
    删除用例不回收编号（沟通口径稳定）、重命名视为新用例。纯展示字段，
    不参与执行链与报告对账（对账仍以 JSON 里的 caseId 为准）。
    """
    meta = {}
    cases_dir = root / "cases"
    if not cases_dir.exists():
        return meta
    with _CASE_NO_LOCK:
        reg_path = root / "artifacts" / ".case_no.json"
        try:
            nos = json.loads(reg_path.read_text(encoding="utf-8"))
            nos = nos if isinstance(nos, dict) else {}
        except (OSError, ValueError):
            nos = {}
        next_no = max([*nos.values(), 0]) + 1
        dirty = False
        for f in sorted(cases_dir.glob("*.json")):
            if f.name not in nos:
                nos[f.name] = next_no
                next_no += 1
                dirty = True
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pkg = (data.get("target") or {}).get("package", "")
            meta[f.name] = {
                "package": pkg,
                "app": pkg,  # 别名：前端/概览按 app 读取（键名曾不匹配 ⇒ 应用列全"—"）
                "title": data.get("name", ""),
                "caseId": data.get("caseId", ""),  # 固定标识：跨改名稳定，报告/日志对账用
                "datasets": bool(data.get("datasets")),
                "no": nos[f.name],
            }
        if dirty:
            reg_path.parent.mkdir(parents=True, exist_ok=True)
            reg_path.write_text(json.dumps(nos, ensure_ascii=False), encoding="utf-8")
    return meta


def list_runs(root: Path) -> list[dict]:
    runs = []
    artifacts = root / "artifacts"
    if artifacts.exists():
        for run_json in artifacts.glob("*/run.json"):
            try:
                data = json.loads(run_json.read_text(encoding="utf-8"))
                run = data.get("run", {})
                run_dir = run_json.parent
                runs.append({
                    "dir": run_json.parent.name,
                    "case_id": run.get("case_id"),
                    "case_name": run.get("case_name"),
                    "package": (data.get("case", {}).get("target") or {}).get("package"),
                    "status": run.get("status"),
                    "rate": run.get("step_success_rate"),
                    # 执行人（发起人 2026-10-12：报告列表/详情按人区分）；旧产物无此字段 → ""
                    "operator": run.get("operator") or "",
                    "finished_at": data.get("finished_at"),
                    # 报告页「分类清晰、数据完备」：
                    "failed_categories": run.get("failed_categories") or {},
                    # 批次标识（套件批量运行才有，报告页据此归组折叠；单条为 None）
                    "batch": run.get("batch"),
                    "diagnosed": (run_dir / "diagnose.json").exists(),
                    "calibrated": (run_dir / "calibration.json").exists(),
                    "human_marked": any(s.get("category_source") == "human"
                                        for s in run.get("steps") or []),
                })
            except (OSError, ValueError):
                continue
    # 按 finished_at 倒序——原「glob 字典序 ≈ 时间序」的假设被批量执行产物目录的
    # 「01_ 前缀」打破（2026-10-13 实测：最新批次排第 51 位，[:50] 裁掉导致新报告不显示）
    runs.sort(key=lambda r: r["finished_at"] or "", reverse=True)
    # 报告名额口径（发起人 2026-10-13）：一个套件批次算 **1 份报告**，组内单条不占
    # 「最近 50」名额——批次跑多了之后，单条用例很快被挤出窗口，反而看不到最近的批次。
    # 遇批次首条即收**全组**条目（前端展开明细用 children），名额按报告计数，满 50 为止。
    out: list[dict] = []
    seen_batch: set[str] = set()
    seen_dir: set[str] = set()
    quota = 0

    def _bid(entry: dict) -> str | None:
        b = entry.get("batch")
        return str(b.get("id")) if isinstance(b, dict) and b.get("id") else None

    for r in runs:
        bid = _bid(r)
        if bid:
            if bid in seen_batch:
                continue                      # 组内其余条目已随首条收过
            seen_batch.add(bid)
            for x in runs:                    # 整组收齐（组员可能在窗口边界之外）
                if _bid(x) == bid and x["dir"] not in seen_dir:
                    seen_dir.add(x["dir"])
                    out.append(x)
        else:
            if r["dir"] in seen_dir:
                continue
            seen_dir.add(r["dir"])
            out.append(r)
        quota += 1
        if quota >= 50:
            break
    return out


def list_imports(root: Path) -> list[dict]:
    imports = []
    base = root / "artifacts" / "imports"
    if base.exists():
        for imp in sorted(base.glob("*/import.json"))[-10:]:
            try:
                data = json.loads(imp.read_text(encoding="utf-8"))
                imports.append({
                    "id": data.get("import_id"),
                    "finished_at": data.get("finished_at"),
                    "source_file": data.get("source_file"),
                    "summary": data.get("summary"),
                })
            except (OSError, ValueError):
                continue
    return list(reversed(imports))


def list_explores(root: Path) -> list[dict]:
    from ..report import _merge_findings  # 惰性导入：复用探索报告的同因合并逻辑

    out = []
    base = root / "artifacts" / "explore"
    if base.exists():
        for f in sorted(base.glob("*/explore.json"))[-10:]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                # findings 计数口径（发起人 2026-10-13）：列表与报告头部一致，用**合并后**
                # 数目（同页/同主干同类的多条算 1 个异常），不再展示合并前原始处数
                raw = [x for x in data.get("findings", []) if isinstance(x, dict)]
                out.append({"id": data.get("explore_id"), "package": data.get("package"),
                            "steps": data.get("steps"), "findings": len(_merge_findings(raw)),
                            # 探索异常有人工校准（确认/忽略）过 ⇒ 报告列表挂「已校准」标记
                            "calibrated": any((x.get("calibration") or {}).get("verdict")
                                              for x in raw),
                            "depth_stats": data.get("depth_stats", {}),
                            # 报告列表按完成时间倒序混排探索报告（发起人 2026-10-12）——
                            # explore.json 落盘时记 generated_at，这里原样透出
                            "finished_at": data.get("generated_at") or "",
                            "duration_seconds": data.get("duration_seconds")})
            except (OSError, ValueError):
                continue
    return list(reversed(out))


def list_indexes(root: Path) -> list[dict]:
    indexes = []
    base = root / "artifacts" / "page_index"
    if base.exists():
        for f in sorted(base.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                pages = data.get("pages")
                if isinstance(pages, dict):  # 防御：旧/异常结构也按 list 计数
                    pages = list(pages.values())
                indexes.append({"package": f.stem, "pages": len(pages or []),
                                "generated_at": data.get("updated_at") or data.get("generated_at", "")})
            except (OSError, ValueError):
                continue
    return indexes
