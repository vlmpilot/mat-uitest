"""执行产物索引（`artifacts/**/run.json`）——**唯一**的扫描/过滤层。

为什么单独一层（2026-11-02 复核）：

- `web_api.workspace.list_runs` 与 `verify_subset.build_history` 原先各自
  `rglob("run.json")`，两套"哪些算日常运行"的判据（`eval/`、日期命名批次）迟早分叉；
- 枚举与 batch 过滤收在这里之后，调用方只决定"要不要含 batch"，不再各写一遍 glob。

⚠️ 本模块只依赖标准库：CLI（`mat verify --subset`）与 MCP 也会导入它，不能把
web_api 的依赖带进来。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

#: 评测跑批的目录前缀：`artifacts/eval/` 与**日期命名**的跑批目录
#: （`2026-10-25-d3-r1/` 这类，是我们自己跑评测的惯例命名）。
_BATCH_DIR_RE = r"^\d{4}-\d{2}-\d{2}"

#: 索引里每个 caseId 保留的最近执行条数（`last-failed` 要回答「最近 3 次」）
HISTORY_KEEP = 3


def is_batch_run(rel: str) -> bool:
    """`rel` 是相对 `artifacts/` 的路径；判断它是不是评测跑批（不该进日常索引）。"""
    if rel.startswith("eval/"):
        return True
    return bool(re.match(_BATCH_DIR_RE, rel.split("/", 1)[0]))


def iter_run_json(root: Path, *, include_batch: bool = False) -> Iterator[Path]:
    """按路径排序枚举全部 `run.json`；默认排除评测跑批。"""
    artifacts = Path(root) / "artifacts"
    if not artifacts.exists():
        return
    for run_json in sorted(artifacts.rglob("run.json")):
        if not include_batch:
            rel = run_json.relative_to(artifacts).as_posix()
            if is_batch_run(rel):
                continue
        yield run_json


#: run.json 精简元数据缓存：`{绝对路径: (mtime, {case_id, status, finished_at})}`。
#: 只存三个字段（2479 份约 1MB），供历史索引复用；**不存完整 doc**——控制台的行缓存
#: 另有一套（它要 run/case 的更多字段）。
_META_CACHE: dict[str, tuple[float, dict]] = {}


def read_meta(run_json: Path) -> dict | None:
    """读取一条 run.json 的精简元数据；坏文件返回 None（不中断整轮扫描）。"""
    key = str(run_json)
    try:
        mtime = run_json.stat().st_mtime
    except OSError:
        return None
    cached = _META_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    meta = {
        "case_id": str((data.get("case") or {}).get("caseId") or ""),
        "status": (data.get("run") or {}).get("status") or "?",
        "finished_at": data.get("finished_at") or "",
    }
    _META_CACHE[key] = (mtime, meta)
    return meta


def clear_meta_cache() -> None:
    """测试/长驻进程切换工作区时清空缓存。"""
    _META_CACHE.clear()


def build_history(root: Path, force: bool = False) -> dict:
    """扫描执行记录，产出 `{caseId: [最近 3 条]}` 的持久索引。

    - 只保留 `{status, finished_at, run_json}`——`last-failed` 只需要这三个；
    - 增量：按 run.json 的 mtime 判断是否要重解析（现网 2479 份全解析 5.62s）；
    - 孤儿回收（2026-11-02 复核）：产物目录被删 / 移进 `eval/` 后，索引里的旧条目
      不能再驱动 `last-failed`；mtimes 同步回收，索引不能只增不减。
    """
    hist_path = Path(root) / "artifacts" / "verify" / "_history.json"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist = _load(hist_path) if not force else {}
    seen = {k: float(v) for k, v in ((hist.get("__meta__") or {}).get("mtimes") or {}).items()}
    out: dict[str, list[dict]] = hist.get("cases") or {}

    present_rel: set[str] = set()
    present_abs: set[str] = set()
    for run_json in iter_run_json(root):
        rel = run_json.relative_to(Path(root) / "artifacts").as_posix()
        present_rel.add(rel)
        present_abs.add(str(run_json))
        try:
            mtime = run_json.stat().st_mtime
        except OSError:
            continue
        if not force and seen.get(rel) == mtime:
            continue
        meta = read_meta(run_json)
        if not meta or not meta["case_id"]:
            continue
        entry = {"status": meta["status"], "finished_at": meta["finished_at"],
                 "run_json": str(run_json)}
        bucket = [e for e in out.get(meta["case_id"], [])
                  if e["run_json"] != entry["run_json"]]
        bucket.append(entry)
        bucket.sort(key=lambda e: e["finished_at"], reverse=True)
        out[meta["case_id"]] = bucket[:HISTORY_KEEP]
        seen[rel] = mtime

    if not force:
        out = {cid: [e for e in entries if str(e.get("run_json")) in present_abs]
               for cid, entries in out.items()}
        out = {cid: entries for cid, entries in out.items() if entries}
        seen = {rel: mtime for rel, mtime in seen.items() if rel in present_rel}

    result = {"__meta__": {"built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                           "mtimes": seen}, "cases": out}
    hist_path.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def _load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
