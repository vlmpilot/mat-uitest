"""/api/review GET/POST（页面复核域）。"""

from __future__ import annotations

import json
from pathlib import Path


def api_review_list(root: Path, package: str) -> tuple[int, dict]:
    """配置台「页面复核」：列出某包图谱里的页面（标题 + 截图 URL）。

    截图直接用 `/artifacts/...` 的 URL 引用 —— console 本身就是 http 服务，
    该前缀已被 do_GET 放行（**不需要**像离线复核页那样内联 base64）。
    """
    from .. import review as review_mod

    if not package or "/" in package or ".." in package:
        return 400, {"error": "非法包名"}
    try:
        index = review_mod.load_index(package, root)
    except FileNotFoundError:
        return 404, {"error": f"没有页面图谱：{package}（先跑 mat index / mat explore）"}

    items = review_mod.build_items(index)
    for it in items:
        it["screenshot_url"] = (f"/artifacts/page_index/{it['screenshot']}"
                               if it["screenshot"] else "")
    # 把已存的复核结论也带上：配置台要显示「哪些是你改过的」
    saved: dict = {}
    store = review_mod.review_path(package, root)
    if store.exists():
        try:
            saved = review_mod.load_corrections(store)["corrections"]
        except (ValueError, json.JSONDecodeError):
            saved = {}
    return 200, {
        "package": package,
        "pages": items,
        "saved": saved,
        "manual": sum(1 for it in items if it["title_source"] == "manual"),
        "review_path": str(review_mod.review_path(package, root).relative_to(root)),
    }


def api_review_save(root: Path, body: dict) -> tuple[int, dict]:
    """保存复核结论：写回图谱（标 `manual`）+ **累积**落盘到 `review/<包名>.json`。

    标题走 `corrections`（page_key → 标题），功能描述走平级的
    `summary_corrections`（page_key → 描述，2026-10-13 起可选）——两者都标
    `manual`，此后 `describe` 与 merge 均不覆盖。
    ⚠️ 找不到的 `page_key` **逐条回给前端**（图谱重跑后 key 会变）——
    静默丢弃等于用户白复核。
    """
    from .. import review as review_mod
    from ..page_index import save_index

    package = str(body.get("package") or "")
    corrections = body.get("corrections")
    summary_corrections = body.get("summary_corrections")
    if not package or "/" in package or ".." in package:
        return 400, {"error": "非法包名"}
    if not isinstance(corrections, dict):
        return 400, {"error": "corrections 必须是对象（page_key → 标题）"}
    if summary_corrections is not None and not isinstance(summary_corrections, dict):
        return 400, {"error": "summary_corrections 必须是对象（page_key → 描述）"}
    try:
        index = review_mod.load_index(package, root)
    except FileNotFoundError:
        return 404, {"error": f"没有页面图谱：{package}"}

    stats = review_mod.apply_corrections(index, corrections, summary_corrections)
    if stats["applied"] or stats["summary_applied"]:
        save_index(review_mod.index_path(package, root), index)
    store = review_mod.review_path(package, root)
    existing: dict = {}
    existing_summaries: dict = {}
    if store.exists():
        try:
            saved = review_mod.load_corrections(store)
            existing = saved["corrections"]
            existing_summaries = saved.get("summary_corrections") or {}
        except (ValueError, json.JSONDecodeError):
            existing = {}
    existing.update(corrections)
    if summary_corrections:
        existing_summaries.update(summary_corrections)
    review_mod.save_review(package, existing, root, note="由控制台「页面复核」写入",
                           summary_corrections=existing_summaries or None)
    return 200, {"ok": True, "applied": len(stats["applied"]),
                 "unchanged": len(stats["unchanged"]),
                 "summary_applied": len(stats["summary_applied"]),
                 "summary_unchanged": len(stats["summary_unchanged"]),
                 "missing": stats["missing"]}
