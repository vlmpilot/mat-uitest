"""碎片合并（A' 单锚点口径）：同逻辑页的 page_key 碎片并成代表页。

## 背景（2026-10-17 dry-run 实测，tx-news 404 页）

同逻辑页被内容轮换/弹窗变体碎片化成多个 page_key（同页稳定性实测仅 12%），
代价散在三处：置备代表页选择靠运气、落点核对靠等价判据兜底、图谱膨胀。
三种口径离线实测定型：

- **A' 单锚点（本模块，推荐）**：链根相同 + activity 全同 + 与代表页 rid
  Jaccard ≥ 0.5 —— 28 组并掉 165 页（缩 41%），组内与代表 minJ ≥ 0.50；
- A 全两两闭包：合并率几乎一致（167 页）但语义弱于单锚点（落地要「并到代表」）；
- B 放宽链根：70 页大组 minJ=0.00（传递闭包链式稀释实锤）且并掉根页
  破坏结构 —— **否决**。

## 单锚点语义

每个闭包组内选**代表页**（rank 与 `arrival_provision.infer_target_page` 同款：
arrival stable 优先 → hits 降序 → page_key 字典序，确定性），只把「与代表
J≥阈值」的成员并入；剔除后剩余成员重新选代表迭代——从根上防链式稀释
（B 口径的教训），且合并即落地语义（保留代表 key，其余 alias 掉）。

## apply 落盘规则

- 代表页保留原 key；被并页从 `pages` 删除，`index["aliases"][旧key]` 留档
  （`{of, min_j, merged_at}`，供查询/后续 explore 侧归并二期用）；
- 所有 `parent_key` / `edges.from/to` 引用重定向到代表（被并入的页不再是
  有效的 parent/边端点）；
- **落点防环（自环继承的推广）**：parent 重定向落点在**自身子树内**时（代表
  自环——祖先碎片被后代代表吸收；或「夹心页」——链上两段同页碎片中间夹的
  不同页，其 parent 被并页重定向拉向自己子树内的代表，tx-news 首轮 apply
  后 135 页成环的实锤根因），沿原 parent 链上移继承到达三元组，链走到头
  则升为根——parent 指向已删页必然断链，指进自身子树则成环，都致命；
  **跨枝互并**（两代表的 parent 互为对方页的碎片，落点都不在对方子树内）
  由终图破环兜底：落点代表 hits 最弱的环节点升根断链、保住强链（②b）；
- 代表页 `hits` 累加成员 hits、`absorbed` 记录成员与各自 minJ（留档可审）；
  `arrival` 证据归并：代表自身无 stable 时搬入成员的 stable 证据；
- 全图 `depth` 按 parent 链重算（重定向后历史脏 depth 一并修正）；
- `from == to` 的自环边丢弃（无导航语义）。

dry-run（缺省）只算不改，返回报告；`apply=True` 就地修改并返回同一报告
（`"applied": true`）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .page_index import EQUIV_SIM

__all__ = ["merge_fragments", "save_report"]


def _rid_set(page: dict) -> frozenset[str]:
    return frozenset(s for s in (page.get("identities") or [])
                     if str(s).startswith("rid:"))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _chain_root(page: dict, pages_by_key: dict[str, dict]) -> str | None:
    """父链走到底的根页 key（防环）。"""
    seen: set[str] = set()
    cur: dict | None = page
    while cur is not None and cur.get("parent_key"):
        if cur["page_key"] in seen:
            return "<cycle>"
        seen.add(cur["page_key"])
        cur = pages_by_key.get(cur["parent_key"])
    return (cur or {}).get("page_key") if cur else None


def _is_below(node_key: str, anc_key: str, pages_by_key: dict[str, dict]) -> bool:
    """node_key 是否落在 anc_key 的子树内（自身或其原始父链上的后代）。

    沿 node 的**原始**父链上移找 anc。合并前图谱无环 ⇒ 判定良定义；
    seen 兜底防脏数据。用于 parent 重定向落点的成环预判。
    """
    seen: set[str] = set()
    cur_key: str | None = node_key
    while cur_key and cur_key not in seen:
        seen.add(cur_key)
        if cur_key == anc_key:
            return True
        cur = pages_by_key.get(cur_key)
        cur_key = cur.get("parent_key") if cur else None
    return False


def _find_parent_cycle(parent: dict[str, str]) -> list[str] | None:
    """终图 parent 映射里找一条环并返回环节点（无环返回 None）。迭代版防深图爆栈。"""
    for start in sorted(parent):
        path: list[str] = []
        pos: dict[str, int] = {}
        k: str = start
        while k in parent and k not in pos:
            pos[k] = len(path)
            path.append(k)
            k = parent[k]
        if k in pos:
            return path[pos[k]:]
    return None


def _representative_rank(page: dict):
    """代表页 rank：stable 优先 → hits 降序 → key 字典序（确定性）。"""
    arrival = page.get("arrival") or {}
    return (1 if arrival.get("status") == "stable" else 0,
            page.get("hits") or 0,
            page["page_key"])


def merge_fragments(index: dict, apply: bool = False,
                    threshold: float = EQUIV_SIM,
                    exclude_activity: tuple[str, ...] = ()) -> dict:
    """图谱碎片合并（A' 单锚点）。dry-run 只算不改；apply 就地修改。

    exclude_activity：activity 含任一子串的页不参与合并（2026-10-17 tx-news
    审查结论：WebView 壳类（WebAdvert/CustomWebBrowser）的 rid 是网页控件
    随机堆叠，J 压线 0.5 可能只是不同网页共享广告框架控件——合并语义存疑，
    宁漏勿错，等有 arrival 证据后再议）。
    """
    if not isinstance(index, dict):
        raise TypeError("index 必须是 dict")
    pages = [p for p in index.get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    pages_by_key = {p["page_key"]: p for p in pages}

    def excluded(page: dict) -> bool:
        act = (page.get("activity") or "")
        return any(sub in act for sub in exclude_activity)

    # ---- 候选与两两关系（链根全等 + activity 全等 + rid J≥阈值）----
    cand = [p for p in pages
            if (p.get("activity") or "").strip() and _rid_set(p)
            and not excluded(p)]
    roots = {p["page_key"]: _chain_root(p, pages_by_key) for p in pages}
    rid_sets = {p["page_key"]: _rid_set(p) for p in pages}
    adj: dict[str, set[str]] = {}
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = cand[i]["page_key"], cand[j]["page_key"]
            if roots[a] != roots[b] or roots[a] in (None, "<cycle>"):
                continue
            if (cand[i].get("activity") or "").strip() != \
                    (cand[j].get("activity") or "").strip():
                continue
            if _jaccard(rid_sets[a], rid_sets[b]) >= threshold:
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)

    # ---- 连通分量（闭包组）→ 组内单锚点迭代拆分 ----
    seen: set[str] = set()
    clusters: list[dict] = []
    for start in sorted(adj):
        if start in seen:
            continue
        component: list[str] = []
        stack = [start]
        seen.add(start)
        while stack:
            k = stack.pop()
            component.append(k)
            for nb in adj[k]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        pool = set(component)
        while pool:
            rep = max(pool, key=lambda k: _representative_rank(pages_by_key[k]))
            members = sorted(
                (k for k in pool if k != rep
                 and _jaccard(rid_sets[rep], rid_sets[k]) >= threshold),
                key=lambda k: -_jaccard(rid_sets[rep], rid_sets[k]))
            clusters.append({
                "representative": rep,
                "members": [{"page_key": k,
                             "j": round(_jaccard(rid_sets[rep], rid_sets[k]), 3)}
                            for k in members],
            })
            pool -= {rep, *members}

    merged_away = sum(len(c["members"]) for c in clusters)
    report = {
        "applied": bool(apply),
        "threshold": threshold,
        "before": len(pages),
        "after": len(pages) - merged_away,
        "merged_away": merged_away,
        "groups": clusters,
        "repointed_parents": 0,
        "repointed_edges": 0,
        "self_parent_inherited": 0,
    }
    if not apply or merged_away == 0:
        return report

    # ---- apply：重定向 → 归并 → 删页 → 重算 depth ----
    now = datetime.now(UTC).isoformat(timespec="seconds")
    # alias / rep_of 先建好（parent、edges 重定向都要查它，④才删页只是落盘顺序）
    rep_of = {m["page_key"]: c["representative"] for c in clusters for m in c["members"]}
    alias: dict[str, dict] = {k: {"of": rep, "merged_at": now}
                              for k, rep in rep_of.items()}

    def redirect(key: str | None) -> str | None:
        if not key:
            return key
        return alias[key]["of"] if key in alias else key

    # ① 代表页归并成员证据（hits 累加 / absorbed 留档）。
    # arrival 不归并：rank 里 stable 优先 ⇒ stable 成员必然当代表，证据天然保留，
    # 不存在「代表缺 stable 而成员有」的可达状态（防御性搬运 = 死代码）。
    for cluster in clusters:
        rep = pages_by_key[cluster["representative"]]
        members = [pages_by_key[m["page_key"]] for m in cluster["members"]]
        rep["hits"] = (rep.get("hits") or 0) + sum(m.get("hits") or 0 for m in members)
        rep["absorbed"] = (rep.get("absorbed") or []) + [
            {"page_key": m["page_key"], "j": m["j"]} for m in cluster["members"]]

    # ② parent 重定向：**先全量计算、后统一落笔**（读-算-写分离）。
    # tx-news 实测教训一：边遍历边就地改写 parent_key，会让先处理页的祖先链被
    # 「抄近道」指向代表；后到的页做继承上移时读到半新半旧的链，A→M→A
    # 人造环 ⇒ while 死循环（dry-run 秒回、apply 卡死几分钟的元凶）。
    # tx-news 实测教训二（首轮 apply 后 135 页成环，forensic 实锤「夹心页」）：
    # 祖先链 root←P←Z←Y←X←R 里 P/Y/X 都并进 R 而 Z 不是——Z 的 parent(P)
    # 重定向拉向 R（落在自身子树内），R 继承上移又恰好落在 Z ⇒ R→Z→R 成环。
    # 统一规则：重定向落点在**自身子树内**（含代表自环特例）时，沿原始父链
    # 上移找子树外落点（或链到头升为根）。原始图无环 ⇒ 子树判定良定义。
    redirected: list[tuple[dict, dict]] = []      # (page, 覆写字段) —— 后统一落笔
    for page in pages:
        old = page.get("parent_key")
        if not old:
            continue
        new = redirect(old)
        fields: dict = {}
        if new and _is_below(new, page["page_key"], pages_by_key):
            # 落点在自身子树内：沿原始父链上移，找 redirect 落在子树外的祖先。
            # redirect/子树检查放 **while 条件**里：停点不得提前进 seen（否则
            # 正常停在子树外会被下面的环判定误吞，单测当场拦下）；seen 从空集
            # 起、第一跳必进循环（old 的 redirect 已知在子树内，要靠它记下
            # 「最后被跳过节点」供动作继承）。
            pk, last_absorbed, seen_pk = old, old, set()
            while (pk and pk not in seen_pk
                   and (r := redirect(pk)) is not None
                   and _is_below(r, page["page_key"], pages_by_key)):
                seen_pk.add(pk)
                last_absorbed = pk
                pk = (pages_by_key.get(pk) or {}).get("parent_key")
            if pk is None:                         # 链走到头：升为根
                new = None
            elif pk in seen_pk:                    # redirect 语义环：放弃继承，退化为根
                report["redirect_cycle_skips"] = \
                    report.get("redirect_cycle_skips", []) + [page["page_key"][:8]]
                new = None
            else:                                  # 子树外落点：继承到达三元组
                new = redirect(pk)
                report["self_parent_inherited"] += 1
                parent_page = pages_by_key.get(last_absorbed) or {}
                fields["parent_action"] = parent_page.get("parent_action") or ""
                fields["parent_rid"] = parent_page.get("parent_rid") or ""
        if new != old:
            report["repointed_parents"] += 1
        if new:
            fields["parent_key"] = new
        else:
            fields["_drop_parent"] = True          # parent 清空（页变根）
        redirected.append((page, fields))

    # ②b 终图破环：**跨枝互并**（两代表的 parent 各是对方页的碎片）时，两个
    # 重定向落点都不在对方原始子树内、②a 管不到，却互相指成环（tx-news 残留
    # 2 环实锤：f.parent=55c5294f（并入 b）、b.parent=607973f0（并入 f））。
    # 对终图 parent 映射做环检测，环上 **自身 hits 最弱**的节点升根断链
    # （升根只丢它自己的到达链，子树不受累——丢弱链保强链，确定性），
    # 循环直到无环（每轮至多摘一个节点，必然终止）。
    final_parent: dict[str, str] = {}
    for page, fields in redirected:
        fp = fields.get("parent_key", page.get("parent_key"))
        if fp:
            final_parent[page["page_key"]] = fp
    broken: set[str] = set()
    while (cycle := _find_parent_cycle(final_parent)) is not None:
        node = min(cycle, key=lambda k: ((pages_by_key[k].get("hits") or 0), k))
        final_parent.pop(node)
        broken.add(node)
    if broken:
        report["cycle_breaks"] = sorted(k[:8] for k in broken)
        for page, fields in redirected:
            if page["page_key"] in broken:
                fields.pop("parent_key", None)
                fields.pop("parent_action", None)
                fields.pop("parent_rid", None)
                fields["_drop_parent"] = True      # 环上弱侧升根（落笔时清父字段）

    for page, fields in redirected:                # 统一落笔（此时 while 早已全部结束）
        if fields.pop("_drop_parent", False):
            page.pop("parent_key", None)
            page.pop("parent_action", None)
            page.pop("parent_rid", None)
        page.update(fields)

    # ③ 边端点重定向；自环边丢弃
    edges = index.get("edges")
    if isinstance(edges, list):
        kept: list[dict] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            f, t = redirect(edge.get("from")), redirect(edge.get("to"))
            if f != edge.get("from") or t != edge.get("to"):
                report["repointed_edges"] += 1
            if not f or not t or f == t:
                continue
            edge["from"], edge["to"] = f, t
            kept.append(edge)
        index["edges"] = kept

    # ④ 删被并页 + aliases 补 min_j 后落盘
    removed = {m["page_key"] for c in clusters for m in c["members"]}
    for k in removed:
        entry = next(m for c in clusters for m in c["members"]
                     if m["page_key"] == k)
        alias[k]["min_j"] = entry["j"]
    index["pages"] = [p for p in index.get("pages", [])
                      if not (isinstance(p, dict) and p.get("page_key") in removed)]
    index["aliases"] = {**(index.get("aliases") or {}), **alias}

    # ⑤ depth 重算（parent 重定向后历史脏 depth 一并修正；BFS 森林遍历）
    remaining = {p["page_key"]: p for p in index["pages"]}
    children: dict[str, list[str]] = {}
    indeg: dict[str, int] = {}
    for p in remaining.values():
        pk = p.get("parent_key")
        if pk and pk in remaining and pk != p["page_key"]:
            children.setdefault(pk, []).append(p["page_key"])
            indeg[p["page_key"]] = indeg.get(p["page_key"], 0) + 1
        else:
            indeg.setdefault(p["page_key"], 0)
    queue = [k for k, d in indeg.items() if d == 0]
    for k in queue:
        remaining[k]["depth"] = 0
    while queue:
        k = queue.pop(0)
        for child in children.get(k, []):
            remaining[child]["depth"] = remaining[k].get("depth", 0) + 1
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)

    index["updated_at"] = now
    return report


def save_report(report: dict, artifacts_dir: Path, package: str) -> Path:
    """报告落盘（dry-run 与 apply 同一格式，供人工审计）。"""
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out = artifacts_dir / f"fragment_merge_{package}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
