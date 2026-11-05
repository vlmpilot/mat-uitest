"""工作区域：清单读取（case_meta/runs/imports/explores/indexes）+
切换/迁移/目录浏览（发起人需求）。

- GET /api/fs?path=        服务端目录浏览器（供"本地文件夹选择窗"；**分享模式下禁用**）
- POST /api/workspace/switch  切换工作区 + 可选迁移（不迁移 / 用例+图谱 / 完整）

运行态设计：工作区根目录挂在 **server 实例**（`srv.ws_root`）上而非 handler 类属性——
同一进程可在线切换；后台运行中的旧任务继续写在旧工作区直到结束（不中断）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from ..run_index import iter_run_json

#: 用例展示编号分配锁（case_meta 可能被并发 state 请求调用，编号不能重发）
_CASE_NO_LOCK = threading.Lock()

#: 进程内编号表：`{工作区绝对路径: {用例文件名: 编号}}`。
#: GET 路径**只读盘、不写盘**（2026-11-05），但编号仍要在同一进程内稳定：
#: 删除不回收、并发不重发、新增续号——全靠这张表承接，落盘交给写接口。
_CASE_NOS_MEM: dict[str, dict[str, int]] = {}

#: 迁移档位 → 要搬的子目录（相对工作区根）。
_MIGRATE_DIRS: dict[str, list[str]] = {
    "none": [],
    "graph": ["cases", "artifacts/page_index"],
    "full": ["cases", "artifacts"],
}


def _local_only(host: str) -> tuple[int, dict] | None:
    """本机能力门控：按【客户端来源 IP】判定——调用方必须传 self.client_address[0]。

    目录浏览/切换/打开、凭据、卸载、校准落盘、doctor 修复等本机能力：
    回环来源放行，局域网/外部来源 403。`--share` 下同事看报告不受影响，
    发起人本机操作也不被连坐（2026-10-15 修复：旧实现误传 server 监听地址，
    0.0.0.0 ⇒ --share 下连本机也 403，与「本机直连时可用」的设计意图相反）。
    """
    if host in ("127.0.0.1", "localhost", "::1"):
        return None
    return 403, {"error": "该操作仅限本机：分享模式下局域网来源禁用，"
                          "请在本机用 http://localhost:7421 访问"}


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

def _case_names(cases_dir: Path) -> list[str]:
    """参与编号的用例文件（按文件名序）；`_batch_` 历史批量临时文件不算。"""
    return sorted(p.name for p in cases_dir.glob("*.json")
                  if not p.name.startswith("_batch_"))


def _read_case_nos(reg_path: Path) -> dict[str, int]:
    """只读注册表；缺失/损坏/无权限一律当空表——**读取方不能因此失败**。"""
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 值可能是手改/历史脏数据（字符串、bool）：非 int 一律丢弃，避免 max() 混类型炸掉
    return {str(k): v for k, v in data.items()
            if isinstance(v, int) and not isinstance(v, bool)}


def _case_nos_key(root: Path) -> str:
    try:
        return str(root.resolve())
    except OSError:
        return str(root)


def _assign_case_nos(nos: dict[str, int], names: list[str]) -> dict[str, int]:
    """给未登记的文件续号（max+1）；已登记的不动 ⇒ 删除不回收、重命名算新用例。"""
    next_no = max(nos.values(), default=0) + 1
    for name in names:
        if name not in nos:
            nos[name] = next_no
            next_no += 1
    return nos


def _merged_case_nos(root: Path, reg_path: Path, names: list[str]) -> dict[str, int]:
    """盘上注册表 + 进程内编号的合并结果（盘上值优先：已持久化的是权威口径）。

    调用方必须已持有 `_CASE_NO_LOCK`。合并结果回写进程内存，保证同一进程内
    多轮 GET 的编号恒定（即使一次都没落过盘）。
    """
    nos = _read_case_nos(reg_path)
    mem = _CASE_NOS_MEM.setdefault(_case_nos_key(root), {})
    for name, no in mem.items():
        nos.setdefault(name, no)
    _assign_case_nos(nos, names)
    mem.update(nos)
    return nos


def case_meta(root: Path) -> dict:
    """用例文件 → {package, title, no} 映射（配置台按 App 分组/联动的数据源）。

    「no」= 展示用例编号 #N（发起人 2026-10-12：caseId 随机字母难沟通且大量重复）。
    编号持久化在 `artifacts/.case_no.json`：删除不回收（沟通口径稳定）、重命名算新用例。
    纯展示字段，不参与执行链与报告对账（对账仍以 JSON 里的 caseId 为准）。

    **GET 不得写盘**（2026-11-05 评审 C3）：只读注册表，读不到就用内存推导；
    落盘只发生在 `persist_case_numbers()`，由显式写接口（保存/生成/自愈用例）调用。
    旧实现每次 GET 发现新用例就 `write_text` —— 只读挂载的工作区一访问就 500。
    """
    meta = {}
    cases_dir = root / "cases"
    if not cases_dir.exists():
        return meta
    names = _case_names(cases_dir)
    with _CASE_NO_LOCK:
        reg_path = root / "artifacts" / ".case_no.json"
        nos = _merged_case_nos(root, reg_path, names)
    for name in names:
        try:
            data = json.loads((cases_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pkg = (data.get("target") or {}).get("package", "")
        meta[name] = {
            "package": pkg,
            "app": pkg,  # 别名：前端/概览按 app 读取（键名曾不匹配 ⇒ 应用列全"—"）
            "title": data.get("name", ""),
            "caseId": data.get("caseId", ""),  # 固定标识：跨改名稳定，报告/日志对账用
            "datasets": bool(data.get("datasets")),
            "no": nos[name],
        }
    return meta


def persist_case_numbers(root: Path) -> bool:
    """把展示编号表落盘。**只由显式写接口调用，GET 永不调用**。

    只读工作区（只读挂载/权限不足/磁盘满）返回 False 而不是抛异常：
    写接口本身已经成功（用例已落盘），编号晚一轮再持久化不影响可用性。
    写盘走临时文件 + `os.replace`：中途失败不会把既有注册表截断成半份。
    """
    cases_dir = root / "cases"
    if not cases_dir.exists():
        return True
    names = _case_names(cases_dir)
    with _CASE_NO_LOCK:
        reg_path = root / "artifacts" / ".case_no.json"
        before = _read_case_nos(reg_path)
        nos = _merged_case_nos(root, reg_path, names)
        if nos == before:
            return True  # 没有新编号，不碰盘（只读部署也不会制造无意义的失败）
        tmp = reg_path.with_name(reg_path.name + ".tmp")
        try:
            reg_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(nos, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, reg_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
    return True


def _verify_summary(run_dir: Path, case_id: str | None = None) -> dict | None:
    """读同目录的 `verify.json`（mat verify 落的判定），裁成列表/卡片够用的形状。

    只带**人要看**的四项：五态 verdict、退出码、失败计数、下一步动作。完整判定仍在
    `verify.json` 里（agent 与复盘看那份），不重复塞进 `/api/state`（5s 轮询，别背大对象）。

    ⚠️ **陈旧判定必须标出来**（2026-11-01 复核）：`verify.json` 与 `run.json` 是两个时刻
    写的，目录可以被后来的运行覆盖（`mat verify --artifacts artifacts/<stem>` 之后又在
    控制台重跑同一条用例——控制台单条跑用的就是固定目录 `artifacts/<stem>`）。实测那种
    现场会渲染成「状态：失败」+「验证结论：通过 exit 0」自相矛盾的一行，正好毁掉这个
    功能存在的理由。判据两条，任一命中就 `stale=True`：
    ① `case_id` 对不上（同目录被另一条用例的判定占用）；
    ② `verify.json` 的 mtime **早于** `run.json`（判定写在更早那次运行上）。
    正常路径（verify 跑完写 run.json 再写 verify.json）两条都不成立。
    """
    path = Path(run_dir) / "verify.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        verify_mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    try:
        run_mtime: float | None = (Path(run_dir) / "run.json").stat().st_mtime
    except OSError:
        # 没有 run.json（运行期崩在落 run.json 之前）⇒ 无从比较新旧，不因此丢掉判定
        run_mtime = None
    actions = [{"category": str(a.get("category") or ""),
                "action": str(a.get("action") or "")}
               for a in (data.get("next_actions") or []) if isinstance(a, dict)]
    own = str(data.get("case_id") or "")
    stale = bool(case_id and own and own != str(case_id))
    if run_mtime is not None:
        stale = stale or verify_mtime < run_mtime
    return {"verdict": data.get("verdict"), "exit_code": data.get("exit_code"),
            "ok": data.get("ok"), "summary": data.get("summary"),
            "next_actions": actions, "stale": stale}


#: `list_runs` 的行缓存：`{run.json 绝对路径: ((mtime, verify, diagnose, calibration), 行)}`。
#:
#: 为什么需要（2026-11-01 实测）：真实工作区 2479 份 run.json，**解析要 6.7 秒**
#: （rglob 只占 0.2 秒），而控制台每 5 秒轮询一次 `/api/state` ⇒ 永远追不上，
#: 界面看着像卡死。缓存键带上**四个会改变这一行的文件 mtime**（run.json 本体、
#: verify.json、diagnose.json、calibration.json），任一变了就重算——
#: 这样「跑完一次诊断/校准」也能立刻反映，不会因为 run.json 没动就显示旧状态。
_RUN_ROW_CACHE: dict[str, tuple[tuple[float, float, float, float], dict]] = {}

#: 行缓存的并发保护（2026-11-02）：控制台是 ThreadingHTTPServer，多个 `/api/state`
#: 请求会并发进 `list_runs`；命中行还要 **复制** 后再交给调用方，避免下游改写缓存。
_RUN_ROW_CACHE_LOCK = threading.Lock()


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def warm_runs_cache(root: Path) -> None:
    """后台预热 `list_runs` 的行缓存（冷启动首屏用）。

    冷启动第一个 `/api/state` 要解析全部 run.json（现网 2479 份实测 7.2s，加设备
    枚举合计 11.8s），而前端每 5s 轮询一次——不预热的话第一屏永远在追。预热失败
    静默：它是优化，不是启动前置条件。
    """
    try:
        list_runs(root)
    except Exception:  # noqa: BLE001 —— 预热失败不影响控制台启动
        pass


def list_runs(root: Path) -> list[dict]:
    runs = []
    artifacts = root / "artifacts"
    if artifacts.exists():
        seen_keys: set[str] = set()
        # P1-6（2026-10-24）：一层 glob 发现不了两层产物——CLI 套件条目
        # `artifacts/<套件>/<NN>_<stem>/run.json`、数据驱动 `artifacts/<stem>-ds/<组名>/`
        # 此前在报告列表**完全不可见**。改 rglob 递归发现；`dir` 从"叶子目录名"
        # 改为 artifacts 相对 posix 路径（一层时与旧值相同 ⇒ 向后兼容，
        # 前端 `/artifacts/${dir}/report.html` 与 diagnose/calibrate 的
        # `root/"artifacts"/run` 都天然支持子路径）。
        for run_json in iter_run_json(root, include_batch=True):
            run_dir = run_json.parent
            cache_key = str(run_json)
            stamp = (_mtime_or_zero(run_json),
                     _mtime_or_zero(run_dir / "verify.json"),
                     _mtime_or_zero(run_dir / "diagnose.json"),
                     _mtime_or_zero(run_dir / "calibration.json"))
            with _RUN_ROW_CACHE_LOCK:
                hit = _RUN_ROW_CACHE.get(cache_key)
            if hit is not None and hit[0] == stamp:
                # 复制行：缓存是进程级共享对象，下游（排序/裁剪/前端序列化）万一改写
                # 会污染后续所有请求（2026-11-02 复核）。
                runs.append(dict(hit[1]))
                seen_keys.add(cache_key)
                continue
            try:
                data = json.loads(run_json.read_text(encoding="utf-8"))
                run = data.get("run", {})
                row = {
                    "dir": run_dir.relative_to(artifacts).as_posix(),
                    "case_id": run.get("case_id"),
                    "case_name": run.get("case_name"),
                    "package": (data.get("case", {}).get("target") or {}).get("package"),
                    # 平台（payload 顶层，runner 落盘时记录）——报告页「重跑」按原平台发起
                    "platform": data.get("platform") or "android",
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
                    # 验证判定（2026-11-01）：`mat verify` 会把 mat.verify/1 落成 verify.json。
                    # 有就带出来——控制台据此渲「验证结论」卡；没有就别塞空对象（旧产物）。
                    # 带 `stale` 标记：目录被后来的运行覆盖过 ⇒ 判定不是这一次的（见函数注释）。
                    "verify": _verify_summary(run_dir, run.get("case_id")),
                }
            except (OSError, ValueError):
                continue
            with _RUN_ROW_CACHE_LOCK:
                _RUN_ROW_CACHE[cache_key] = (stamp, row)
            seen_keys.add(cache_key)
            runs.append(row)
        # 清掉本 root 下已消失的条目：长驻进程不能只增不减（删产物目录很常见）
        prefix = str(artifacts) + os.sep
        with _RUN_ROW_CACHE_LOCK:
            for stale_key in [k for k in _RUN_ROW_CACHE
                              if k.startswith(prefix) and k not in seen_keys]:
                _RUN_ROW_CACHE.pop(stale_key, None)
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
