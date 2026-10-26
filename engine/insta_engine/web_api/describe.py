"""describe 域：/api/describe（AI 给图谱页面补标题/描述，1 次调用 ≤limit 页缺省 50；额度由服务端计量）。"""

from __future__ import annotations

from pathlib import Path

from .. import promptstore
from ..ai_client import GenClient
from ..quota import UsageStore
from .runtime import _require_fresh_code, operator_of, start_job


def _targets(root: Path, package: str, limit: int, refresh: bool) -> list[dict]:
    """取待描述页面（AI 不顶人工：跳过 manual 标题/**manual 描述**；refresh 强制重算）。无图谱抛 RuntimeError。"""
    from ..page_index import load_index

    index = load_index(root / "artifacts" / "page_index" / f"{package}.json")
    if index is None:
        raise RuntimeError(f"没有页面图谱——先构建页面索引（{package}）")
    pages = index.get("pages", [])
    if not pages:
        raise RuntimeError("图谱里还没有页面（先 index 或 explore）")
    targets = [p for p in pages if refresh or p.get("summary_source") not in ("ai", "manual")]
    return [p for p in targets if p.get("title_source") != "manual"][:limit]


def _payload(targets: list[dict]) -> list[dict]:
    return [{"page_key": p.get("page_key"), "depth": p.get("depth"),
             "parent_action": p.get("parent_action"), "activity": p.get("activity", ""),
             "key_elements": (p.get("structure") or {}).get("key_elements", []),
             "layout_regions": (p.get("structure") or {}).get("layout_regions", []),
             "dynamic_regions": (p.get("structure") or {}).get("dynamic_regions", [])}
            for p in targets]


def api_describe(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/describe：与 CLI `mat describe` 同语义。

    - 只描述 `summary_source != "ai"` 且 `title_source != "manual"` 的页面；
    - **本地不预检额度**（P0-2 起生成不再本地拦截）：成功落盘后才 `consume()` 计数，
      失败不扣；真正的额度上限由服务端计量；
    - 无图谱在端点内 400 快速失败（不开 job）；单次 AI 调用不可中途取消。
    """
    package = str(body.get("package", "")).strip()
    if not package:
        return 400, {"error": "需要 package"}
    # 与 api_explore 同闸口（A3）：包名会拼进 artifacts/page_index/<package>.json
    # 路径并写回（save_index），形状非法（../、绝对路径、盘符）在边界拒绝。
    from ..pkgname import safe_package_name

    try:
        safe_package_name(package)
    except ValueError as exc:
        return 400, {"error": f"包名不合法: {exc}"}
    limit = min(max(int(body.get("limit", 50)), 1), 200)
    refresh = bool(body.get("refresh"))

    try:
        targets = _targets(root, package, limit, refresh)
    except RuntimeError as exc:
        return 400, {"error": str(exc)}
    if not targets:
        return 200, {"ok": True, "noop": True,
                     "message": "所有页面都已有标题或描述，无需 AI 补全（人工内容不会被覆盖）"}
    payload = _payload(targets)

    def job(emit, cancel):
        from ..page_index import save_index, update_page

        _require_fresh_code()
        usage = UsageStore()
        # P0-2：本地不再预检（ensure_capacity 已退化为纯快照、永不抛）——
        # 官方代理由服务端 429 计量；这里的 usage 仅用于成功后本地计数展示
        emit("describe", "running", f"描述 {len(targets)} 个页面（1 次 AI 调用，额度由服务端计量）")
        described = GenClient.from_env().describe_pages(payload)
        snapshot = usage.consume()  # 成功落盘前才核销

        index_path = root / "artifacts" / "page_index" / f"{package}.json"
        updated = 0
        # 重新载入（job 排队期间图谱可能被 explore/index 更新过——不整份覆盖内存旧份）
        from ..page_index import load_index
        index = load_index(index_path) or {}
        for page in targets:
            info = described.get(page.get("page_key"))
            if not info or not info.get("title"):
                continue
            update_page(index, page["page_key"], title=info["title"], title_source="ai",
                        summary=info.get("summary", ""), summary_source="ai")
            updated += 1
        save_index(index_path, index)
        if promptstore.is_server():
            limit_text = snapshot["gen_limit"] if snapshot["gen_limit"] is not None else "不限"
            usage_tail = f"（本月生成用量 {snapshot['gen_count']}/{limit_text}）"
        else:
            # 自带 Key：token 由用户自己的服务商计费，不存在"平台额度"
            usage_tail = "（自带 Key，不占平台额度）"
        return (f"已补 {updated}/{len(targets)} 个页面标题{usage_tail}"), None

    return 200, {"job_id": start_job("describe", job, meta={"operator": operator_of(body)})}
