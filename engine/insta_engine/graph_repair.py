"""图谱断链静态修复：不连真机，把 arrival 无法重放的断链接回来（零 AI/真机成本）。

## 问题（实测 tx-news 图谱，2026-10-16）

depth ≥ 1 的 99 页里 **75 条父链断**，全部断在缺 `parent_action`（parent 页本身
都在图谱）。根因是 explore 的 back/直达到达（`_record` 记 `parent_action=None`）
在「更浅路径更新」时**覆盖掉了原本可重放的点击链**——`mat arrival` 与执行期置备
都靠父链重放，断一条链就少一个可达页面（覆盖缺口分析的「图谱里有、够不着」）。

## 两个修复器（按证据强度排序，每页只用其一）

| 修复器 | 证据 | 说明 |
|---|---|---|
| A **alt_entries 提升** | 页面自己的多入口记录 | alt 条目 `(parent_key, parent_action)` 就是「从另一入口也能到达」的事实；提升为父链并把 depth 改成 `父深+1`。实测可救 9/75 |
| B **同逻辑页借链** | 碎片等价 | 同 activity、identities Jaccard ≥ 阈值、自身链完整的兄弟碎片，把它的父链借给断链页（记 `alias_of`/`alias_sim` 留档）。实测上限 50/75 |

两条都必须通过 **整链校验**（`_chain_of` 非 None 且 ≤6 跳）才落笔——修复不制造
新的不可重放链。落点核对/状态核对已升级为「同逻辑页碎片等价」
（`page_index.pages_equivalent`），借来的链在重放时最后一跳落到兄弟碎片不再误报。

## 边界（刻意收窄）

- **纯静态**：不猜不造——alt 里没有、图谱里也没有相似碎片 ⇒ 记入 `unresolved`；
  要真机重探请直接再跑一轮 `mat explore`（配合 `_record`/`_merge_page` 的
  「浅到达不抹可重放链」止血，新链不会被 back 再抹掉）；
- **瞬态启动帧根不修**（2026-10-16）：链根含「跳过」类一次性控件（splash/开屏
  广告，`page_index.is_transient_frame`）的链视为断链参与修复——语义可重放但真机
  结构性不可重放（launch 后根帧被自动翻过，第 1 跳是竞态）；alt 提升与借链的
  **修复后根也不得是瞬态帧**，否则等于把不可重放链标记为已修复；
- 借链选**证据最强**的兄弟（Jaccard 最高；并列取 depth 更浅——重放便宜）；
- 不改 page_key、不动 controls：碎片合并是更大的手术，等价判据已够用。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .arrivals import _MAX_PATH_LEN, _chain_of, _root_page_of
from .page_index import (
    _ident_jaccard,
    hop_replayable,
    is_ad_landing,
    is_transient_frame,
    load_index,
    root_reachable,
    save_index,
)
from .safety import is_back_rid, is_back_word, is_splash_skip_label

__all__ = ["repair", "DEFAULT_MIN_SIM"]

#: 借链的「同逻辑页」Jaccard 门槛（与 pages_equivalent 同一挡位）。
DEFAULT_MIN_SIM = 0.5


def _root_unreachable(page: dict, pages_by_key: dict,
                      launch_activity: str | None) -> bool:
    """父链根是否不可达：瞬态启动帧（splash「跳过」）或 launch 落点不符。

    ⚠️ **借链假修复的教训（2026-10-16 真机）**：launch 不可达根的链「语义可重放」
    是假的——第一版只排瞬态帧，结果 56 条瞬态根链借到了遗留播客根的链上，
    **换链不换根**，真机 0/20 全在根核对处拦下。可达性必须按 launch 落点判。
    """
    root = _root_page_of(page, pages_by_key)
    if root is None:
        return False
    return is_transient_frame(root) or not root_reachable(root, launch_activity)


def _hop_ok(hop: dict) -> bool:
    """一跳的语义可重放：非回退语义，且有可重放通道（rid 或非 iconfont 文案）。

    iconfont 私有区字符看着「有文案」，重放按文案匹配必死（第 4 层实测 0/25）——
    判据与 explore._record / _merge_page 的换链升级同一来源（page_index.hop_replayable）。
    """
    return (not is_back_word(hop["action"]) and not is_back_rid(hop.get("rid"))
            and hop_replayable(hop.get("action") or "", hop.get("rid") or ""))


def _replayable(page: dict, pages_by_key: dict) -> bool:
    """父链**语义可重放**：结构完整（≤6 跳）且链上没有回退/关闭语义的动作。

    回退词判据与 explore 选点过滤同一来源（safety.is_back_word）——explore 不再
    产生回退链，但存量图谱里已有（explore 曾在首页点「返回」到达新页），借链
    还会把它们扩散给相似碎片（实测：repair 后新鲜页的链首跳变成「返回」）。
    """
    chain = _chain_of(page, pages_by_key)
    if not chain or len(chain) > _MAX_PATH_LEN:
        return False
    return all(_hop_ok(hop) for hop in chain)


def _promote_splash_children(pages_by_key: dict,
                             launch_activity: str | None = None) -> list[dict]:
    """把瞬态启动帧根的「跳过」子页**提升为根**（splash 是环境噪声，不是导航链）。

    实测（2026-10-16 真机）：tx-news 冷启动**必经**开屏广告帧（含「跳过」，
    activity 与首页同为 SplashActivity），有效首页永远是它的 L1 子页 ⇒ explore
    从 launch 落点建图，所有链的根天然是瞬态帧——「如实 skip」只是把不可重放
    如实记下来，重放闭环还得靠**根上移**：经「跳过」到达的 L1 页（真实首页）
    提升为根，launch →（splash 由重放前置统一翻掉）→ 首页起链。

    两道收紧：
    - 只提升 parent_action 是跳过词的子页：「不感兴趣」等别的动作到达的页不是
      有效入口（广告帧下乱点的产物）；
    - 传入 `launch_activity` 时，提升页 activity 必须 = 有效首屏——「跳过」点击
      可能**误触广告跳转**（实测落到了视频详情页暗色变体），activity 对不上的一律
      不提升（宁可漏提升，不可造假根）。

    提升后全图 depth 按根 BFS 重算。
    """
    promoted: list[dict] = []
    for p in pages_by_key.values():
        if not isinstance(p.get("depth"), int) or p["depth"] != 1:
            continue                      # 只看 L1（瞬态根的直接子页）
        root = pages_by_key.get(p.get("parent_key") or "")
        if root is None or not is_transient_frame(root):
            continue
        if not is_splash_skip_label(p.get("parent_action") or ""):
            continue                      # 只有「跳过」语义的入口才是有效首屏
        if launch_activity and (p.get("activity") or "").strip() != launch_activity:
            continue                      # activity 对不上：广告跳转产物，不是首页
        promoted.append({"page_key": p["page_key"], "from_root": root.get("page_key"),
                         "action": p.get("parent_action") or ""})
        p["depth"] = 0
        p["parent_key"] = None
        p["parent_action"] = None
        p["parent_rid"] = None
    if promoted:
        _recompute_depths(pages_by_key)
    return promoted


def _recompute_depths(pages_by_key: dict) -> None:
    """从全部根 BFS 重算 depth（提升根后子树整体上移一层）。

    根 = depth-0 **且无 parent**——只按 depth 判会把「刚恢复 parent 但 depth
    还是 0 的页」误当种子（实测：撤销误提升时它自己成了自己的根）。
    """
    kids: dict[str, list[dict]] = {}
    for p in pages_by_key.values():
        pk = p.get("parent_key") or ""
        if pk in pages_by_key:
            kids.setdefault(pk, []).append(p)
    seen: set[str] = set()
    stack = [(p, 0) for p in pages_by_key.values()
             if isinstance(p.get("depth"), int) and p["depth"] == 0
             and not p.get("parent_key")]
    while stack:
        node, d = stack.pop()
        key = node.get("page_key") or ""
        if key in seen:
            continue                      # 防脏数据成环
        seen.add(key)
        if node.get("depth") != d:
            node["depth"] = d
        for k in kids.get(key, []):
            stack.append((k, d + 1))


def _hop_visible(hop: dict, src: dict | None) -> bool:
    """单跳控件是否出现在源页控件清单（rid 按 res-id、action 按 text/desc）。

    源页没有 controls（旧图谱/爬取时未存）⇒ 无法校验，视为可见（不表态 ≠ 否定，
    避免旧数据把借链大批误杀）。真机教训（2026-10-17 批量验证 2/8 unstable）：
    懒加载控件不在首屏 dump 里，链上写着重放必死于「未找到可点控件」。
    """
    controls = (src or {}).get("controls") or []
    if not controls:
        return True
    rid = hop.get("rid") or ""
    action = hop.get("action") or ""
    for c in controls:
        if rid and c.get("res-id") == rid:
            return True
        text = (c.get("text") or "").strip()
        desc = (c.get("desc") or "").strip()
        if action and action and (action == text or action == desc
                                  or (text and action in text)
                                  or (desc and action in desc)):
            return True
    return False


def _chain_visible(chain: list[dict], pages_by_key: dict) -> bool:
    """链上每跳的控件都在其**点击源页**的 controls 里（hop i 的源页 = 上一层）。"""
    for i, hop in enumerate(chain):
        if i == 0:
            root = pages_by_key.get(hop["expected_key"])
            while root is not None and root.get("parent_key"):
                root = pages_by_key.get(root["parent_key"])
            src = root
        else:
            src = pages_by_key.get(chain[i - 1]["expected_key"])
        if not _hop_visible(hop, src):
            return False
    return True


def repair(index: dict, min_sim: float = DEFAULT_MIN_SIM) -> tuple[dict, dict]:
    """修复图谱里的断链父链（就地修改 pages 并返回 `(index, report)`）。

    报告：`{"broken", "fixed_by_alt", "fixed_by_alias", "unresolved",
    "promoted", "changes": [...]}`——每笔修复留 `via`/`alias_of`/`alias_sim`，
    可人工复核。

    先行步骤：**瞬态根「跳过」子页提升为根**（`_promote_splash_children`）——
    冷启动必经开屏帧的 App（tx-news 实测），有效首页永远是瞬态帧的 L1 子页，
    不提升则其下所有链在 verify 永远 skip、repair 永远 unresolved。
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    launch_act = index.get("launch_activity") or ""
    pages = [p for p in index.get("pages", []) if isinstance(p, dict) and p.get("page_key")]
    pages_by_key = {p["page_key"]: p for p in pages}
    promoted = _promote_splash_children(pages_by_key, launch_act)
    # **广告落地页既不当修复目标，也不当 donor**（2026-10-23 实测）：它们不是被测 App
    # 的功能页，点进去是信息流广告的随机落地页。留在 donor 池里的后果很具体——
    # tx-news 那 4 条借链里有 3 条借到了「点 天猫双11热卖中」，把不可重放的路径标成已修复。
    noise = [p for p in pages if is_ad_landing(p)]
    routable = [p for p in pages if not is_ad_landing(p)]
    targets = [p for p in routable
               if isinstance(p.get("depth"), int) and p["depth"] >= 1]
    # ⚠️ **扁平图谱（无任何 depth≥1 的页）必须单独报出来**：`mat index` 建的是
    # "只记页面、不记怎么到达"的清单，这种图谱 `targets` 为空 ⇒ broken=0，
    # 读起来像"很健康"，实际是"根本没有层级，无从修起"。
    # 2026-10-21 实测：tx-news 图谱 sources=["index"]、16 页全 depth=None，
    # repair 报"断链 0 条"，而真机 10 条用例的前置置备全因"父链不完整"失败。
    flat = not any(isinstance(p.get("depth"), int) for p in pages)
    # **depth 为空的页也要修**（2026-10-23）：它们以前既不在 targets（所以不进 broken）、
    # 也不在 donor 池里 ⇒ **没人管**，而执行期置备对它们只会报「父链不完整」。
    # 实测 tx-news：53 页里 16 页 depth 为空，其中 10 页能被树状孪生页认回（≥0.5）。
    # 只收有 identities 的（一个稳定信号都没有的页连"像不像"都判不了，修不了）。
    flat_pages = [p for p in routable
                  if p.get("depth") is None and (p.get("identities") or [])]
    broken = [p for p in targets if not _replayable(p, pages_by_key)
              or _root_unreachable(p, pages_by_key, launch_act)]
    broken_flat = [p for p in flat_pages if not _replayable(p, pages_by_key)
                   or _root_unreachable(p, pages_by_key, launch_act)]

    # 借链候选只从「当前链完整且语义可重放」的页里挑（修复不制造新断链/不扩散
    # 回退链）；**不可达根（瞬态帧 / launch 落点不符）的链排除**——它的可重放性
    # 是假的，借出去等于把不可重放链标记为已修复（2026-10-16 真机实锤的假修复）。
    # alt 提升可能让更多页变完整，但候选集按进入修复前算——简单、可复核。
    whole: list[tuple[dict, list[dict]]] = []
    for p in targets:
        chain = _chain_of(p, pages_by_key)
        if chain and len(chain) <= _MAX_PATH_LEN \
                and all(_hop_ok(hop) for hop in chain) \
                and not _root_unreachable(p, pages_by_key, launch_act):
            whole.append((p, chain))

    changes: list[dict] = []
    unresolved: list[dict] = []
    n_alt = n_alias = 0
    for p in broken + broken_flat:
        fixed = False
        # 修复器 A：alt_entries 提升（页面自己的事实，证据最强）
        for alt in p.get("alt_entries") or []:
            if not isinstance(alt, dict):
                continue
            pk, action = alt.get("parent_key"), alt.get("parent_action")
            rid = alt.get("parent_rid") or ""
            if not hop_replayable(action, rid) or pk not in pages_by_key \
                    or is_back_word(action) or is_back_rid(rid):
                continue
            cand = dict(p, parent_key=pk, parent_action=action, parent_rid=rid)
            chain = _chain_of(cand, pages_by_key)
            if not chain or len(chain) > _MAX_PATH_LEN \
                    or not all(_hop_ok(hop) for hop in chain) \
                    or _root_unreachable(cand, pages_by_key, launch_act):
                continue
            parent = pages_by_key[pk]
            p["parent_key"] = pk
            p["parent_action"] = action
            p["parent_rid"] = rid
            p["depth"] = (parent.get("depth") or 0) + 1
            changes.append({"page_key": p["page_key"], "via": "alt_entries",
                            "parent_key": pk, "parent_action": action,
                            "parent_rid": rid, "depth": p["depth"]})
            n_alt += 1
            fixed = True
            break
        if fixed:
            continue
        # 修复器 B：同逻辑页借链（碎片等价）。donor 链还需**途经控件可见**：
        # donor 链上每跳的控件必须出现在该跳点击源页的 controls 里——懒加载控件
        # 不在首屏 dump，链上写着重放必死于「未找到可点控件」（2026-10-17 批量验证
        # 2/8 unstable 实锤）。alt 是页面自己的到达事实，不受此校验。
        best: tuple[dict, float] | None = None
        for q, q_chain in whole:
            if q is p or (q.get("activity") or "") != (p.get("activity") or ""):
                continue
            sim = _ident_jaccard(p, q)
            if sim < min_sim:
                continue
            if not _chain_visible(q_chain, pages_by_key):
                continue
            if best is None or sim > best[1]:
                best = (q, sim)
        if best is None:
            unresolved.append({"page_key": p["page_key"],
                               "reason": f"alt 无可用条目且同 activity 无 J≥{min_sim} "
                                         "且途经控件可见的完整链碎片"})
            continue
        q, sim = best
        p["depth"] = q.get("depth")
        p["parent_key"] = q.get("parent_key")
        p["parent_action"] = q.get("parent_action")
        p["parent_rid"] = q.get("parent_rid")
        p["alias_of"] = q["page_key"]
        p["alias_sim"] = round(sim, 3)
        changes.append({"page_key": p["page_key"], "via": "alias",
                        "parent_key": p["parent_key"],
                        "parent_action": p["parent_action"],
                        "parent_rid": p.get("parent_rid"),
                        "depth": p["depth"], "alias_of": q["page_key"],
                        "alias_sim": round(sim, 3)})
        n_alias += 1

    index["updated_at"] = now
    edges = index.get("edges") or []
    collapsed = collapse_activity_variants(pages_by_key, edges)
    if collapsed:
        # collapse 从 pages_by_key 里 pop 了折叠页——index["pages"] 列表必须同步；
        # edges 就地重映射（P0-3 ①：折叠后 from/to 不得指向已折叠 key）；
        # folds 落盘（留痕含 merged_into，审计可回溯）。
        index["pages"] = list(pages_by_key.values())
        index["edges"] = edges
        index.setdefault("folds", [])
        index["folds"].extend(collapsed)
    report = {"package": index.get("package") or "",
              "generated_at": now, "min_sim": min_sim, "flat": flat,
              "sources": index.get("sources") or [],
              "broken": len(broken), "broken_flat": len(broken_flat),
              "flat_pages": len(flat_pages),
              "noise_pages": len(noise),
              "noise_keys": [p["page_key"] for p in noise][:20],
              "fixed_by_alt": n_alt,
              "fixed_by_alias": n_alias,
              "promoted": promoted,
              "collapsed_variants": collapsed,
              "unresolved": unresolved, "changes": changes}
    return index, report


#: 一个 activity 组要被视为「同一逻辑页的多变体」的门槛（2026-10-25 覆盖率口径修复）。
#: 实测语料：某团购 App 的首页 activity 占 130/223 页、tx-news(资讯 App) 21/44 页；
#: 门槛取 5 避开 NewsDetail×2 这类真实不同内容的偶发对。
#: 容器型 activity：一个 activity 里跑**多种页面**的宿主
#: （Hippy/WebView/RN/Shell/Flutter…）。这类组**禁止整组折叠**——组内还要按
#: identities 相似度二次分簇（实测 tx-news 的 Hippy 组同时装着「搜索页」和
#: 「资讯详情页」，整组折叠等于把两种页型并成一种）。
CONTAINER_ACTIVITY_MARKERS = (
    "hippy", "webview", "web", "reactnative", "rn", "shell", "flutter",
    "x5", "h5", "yoda", "router",
)
#: 容器组内二次分簇的相似度阈值：与 repair 借链的 DEFAULT_MIN_SIM 同源(0.5)——
#: 同一数值语义（"像不像同一页"），不另起口径。
CONTAINER_CLUSTER_SIM = DEFAULT_MIN_SIM

VARIANT_GROUP_MIN = 5


def _union_controls(a: list, b: list) -> list:
    """控件并集，按 (text, bounds, rid) 去重——keeper 保留变体的全部可操作面。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for c in [*a, *b]:
        if not isinstance(c, dict):
            continue
        key = (str(c.get("text") or ""), str(c.get("bounds") or ""), str(c.get("rid") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _merge_entries(a: list, b: list) -> list:
    """alt_entries 并集，按内容键去重（与 page_index._dedup_entries 同思路）。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in [*a, *b]:
        if not isinstance(e, dict):
            continue
        key = tuple(sorted((k, str(v)) for k, v in e.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _is_container_activity(act: str) -> bool:
    return any(m in act.lower() for m in CONTAINER_ACTIVITY_MARKERS)


def _cluster_by_identity(members: list[dict], min_sim: float) -> list[list[dict]]:
    """容器组内按 identities Jaccard 分簇（并查集）。

    同一容器 activity 里跑着**不同页型**（tx-news 的 Hippy 同时装着搜索页与
    资讯详情页）——整组折叠会并掉页型。相似度 ≥ min_sim 的并为同簇，
    簇内才允许折叠；两簇之间互不动。
    """
    parent = {id(m): id(m) for m in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(members):
        for b in members[i + 1:]:
            if _ident_jaccard(a, b) >= min_sim:
                parent[find(id(b))] = find(id(a))
    clusters: dict[int, list[dict]] = {}
    for m in members:
        clusters.setdefault(find(id(m)), []).append(m)
    return list(clusters.values())


def collapse_activity_variants(pages_by_key: dict,
                               edges: list | None = None,
                               min_sim: float = CONTAINER_CLUSTER_SIM) -> list[dict]:
    """**同 activity 变体折叠**（2026-10-25 收尾版）。

    实测病灶：首页类页面的**内容态**（换 Tab/刷新/渐进加载）让结构哈希各不相同
    ⇒ 同一逻辑页被建成几十上百个 page_key（某团购 App 首页 activity 占 130/223、
    tx-news 首页 activity 21/44——后者就是首页本身，见 `is_transient_frame`
    的 2026-10-17 教训：**splash 命名 ≠ 瞬态**，本函数因此折叠而**不删除**）。
    后果：覆盖率的分母被变体灌水，一切下游口径失真。

    判据（保守，由构造保证不误伤内容页）：
    - 组内页数 ≥ VARIANT_GROUP_MIN，**且**存在锚点（hits≥2 的常驻页，或
      depth==0 的 launch 落点根）——锚点证明这是「一个被反复回到的页」；
    - 只折叠组内 hits≤1 的变体；任何 hits≥2 的页一律保留；
    - **容器型 activity**（Hippy/WebView/RN/Shell/Flutter…，P0-3 ③）：组内先按
      identities Jaccard ≥ min_sim 二次分簇，**簇内**才折叠——禁止跨页型合并
      （tx-news 的 Hippy 组同时装着搜索页与资讯详情页，整组折叠=并掉页型）；
    - 纯内容实例组（全 hits==1、无锚点）不满足锚点条件，天然不折叠。

    折叠动作：keeper 吸收变体的 controls（按 text+bounds+rid 去重并集）、
    tried / alt_entries / identities 并集、hits 累加、first/last_seen 取宽；
    变体的子页改挂 keeper（parent_action 保留——从首页点同一入口仍成立）；
    keeper 自身若挂在被折叠页上则升为根；**edges 的 from/to 随折叠重映射并去重**
    （P0-3 ①：折叠前 tx-news 27/52 条边悬空）；**折叠留痕落盘由调用方写进
    index["folds"]**（P0-3 ②：每条 fold 记录含 merged_into，可审计可回溯）；
    最后全图 depth 重算。

    留痕字段（P0-6，2026-10-26）：每条 fold 记录除 `merged_into` 外还有
    `folded[]`（每个被折叠页的 `page_key/title/hits/controls` **+ `identities`
    与 `similarity`**）、`min_similarity`（簇内最勉强的那一刀）、
    `children_relinked`（**本簇**改挂的子页数）。加这三样的理由：实测
    「112 页并成 1 页」这种大合并原先**无法事后复核**——只有 page_key 列表，
    看不出是"同一逻辑页的变体"还是"把不同页型并掉了"；而 `children_relinked`
    在多簇时恒为 0，"改挂成功"与"根本没改挂"长得一模一样。
    """
    import collections
    import datetime as _dt

    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for p in pages_by_key.values():
        act = (p.get("activity") or "").strip()
        if act:
            groups[act].append(p)

    folded_keys: set[str] = set()
    keeper_of: dict[str, str] = {}
    report: list[dict] = []
    #: 被折叠页 → 它所属的 fold 记录。用于把 `children_relinked` 归到**具体那一条**
    #: fold 上（原先 `len(report)==1` 才写值、多条一律写 0，等于多条簇时字段恒 0）。
    fold_owner: dict[str, dict] = {}

    def _fold_cluster(act: str, members: list[dict]) -> None:
        anchors = [p for p in members if int(p.get("hits") or 0) >= 2
                   or p.get("depth") == 0]
        if not anchors:
            return
        anchor_keys = {a["page_key"] for a in anchors}
        variants = [p for p in members
                    if int(p.get("hits") or 0) <= 1 and p["page_key"] not in anchor_keys]
        if not variants:
            return
        keeper = max(anchors, key=lambda p: (int(p.get("hits") or 0),
                                             len(p.get("controls") or [])))
        fold_entry = {
            "keeper": keeper["page_key"], "keeper_title": keeper.get("title") or "",
            "activity": act, "merged_into": keeper["page_key"],
            "at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            "folded": [],
        }
        # ⚠️ 必须在并集**之前**快照 keeper 的 identities：循环里 keeper["identities"]
        # 会被逐个变体撑大，拿撑大后的集合算 Jaccard 会让后面的变体得到**虚高**的
        # 相似度（分母与交集同时变），而 similarity 正是事后复核分簇的唯一依据。
        keeper_ids = {str(s) for s in (keeper.get("identities") or [])}

        def _sim(v: dict) -> float:
            """与 `page_index._ident_jaccard` 同算法，但用上面的快照。"""
            vids = {str(s) for s in (v.get("identities") or [])}
            if not keeper_ids or not vids:
                return 0.0
            return len(keeper_ids & vids) / len(keeper_ids | vids)

        sims: list[float] = []
        for v in variants:
            sim = _sim(v)
            sims.append(sim)
            keeper["controls"] = _union_controls(keeper.get("controls") or [],
                                                 v.get("controls") or [])
            keeper["tried"] = sorted(set(keeper.get("tried") or [])
                                     | set(v.get("tried") or []))
            keeper["identities"] = sorted(set(keeper.get("identities") or [])
                                          | set(v.get("identities") or []))
            keeper["alt_entries"] = _merge_entries(keeper.get("alt_entries") or [],
                                                   v.get("alt_entries") or [])
            keeper["hits"] = int(keeper.get("hits") or 0) + int(v.get("hits") or 0)
            for t_ in ("first_seen", "last_seen"):
                if v.get(t_):
                    cur = keeper.get(t_) or ""
                    keeper[t_] = min(cur, v[t_]) if t_ == "first_seen" else max(cur, v[t_])
            keeper["node_count"] = len(keeper["controls"])
            folded_keys.add(v["page_key"])
            keeper_of[v["page_key"]] = keeper["page_key"]
            fold_owner[v["page_key"]] = fold_entry
            fold_entry["folded"].append({
                "page_key": v["page_key"], "title": v.get("title") or "",
                "hits": v.get("hits", 1), "controls": len(v.get("controls") or []),
                # P0-6（2026-10-26）：留痕要能**事后复核分簇**。原先只有
                # page_key/title/hits/controls，「112 页并成 1 页」这类大合并
                # 无法验证是"同一逻辑页的变体"还是"把不同页型并掉了"。
                # identities 存完整集合（不截断）——它是复核的原始证据；
                # 规模由「被折叠页数」决定，folds 本身每个簇只有一条。
                "identities": list(v.get("identities") or []),
                "similarity": round(sim, 3),
            })
        # 簇内最小相似度：一次看尽"这批折叠最勉强的那一刀"，不必逐条翻
        fold_entry["min_similarity"] = round(min(sims), 3) if sims else None
        fold_entry["children_relinked"] = 0
        # keeper 不能挂在被折叠的变体上（否则自指）
        if keeper.get("parent_key") in folded_keys:
            keeper["parent_key"] = None
            keeper["parent_action"] = None
            keeper["parent_rid"] = None
        report.append(fold_entry)

    for act, members in groups.items():
        if len(members) < VARIANT_GROUP_MIN:
            continue
        if _is_container_activity(act):
            for cluster in _cluster_by_identity(members, min_sim):
                _fold_cluster(act, cluster)
        else:
            _fold_cluster(act, members)

    if folded_keys:
        # P0-6：`children_relinked` 按 fold **分别**统计。原实现是
        # `relinked if len(report) == 1 else 0` —— 多条簇时字段恒为 0，
        # 于是"改挂成功"和"根本没改挂"在留痕里长得一模一样（比不记更坏）。
        for p in pages_by_key.values():
            old_parent = p.get("parent_key")
            if old_parent in folded_keys and p["page_key"] not in folded_keys:
                new_parent = keeper_of.get(old_parent)
                if new_parent and new_parent != p["page_key"]:
                    p["parent_key"] = new_parent
                    owner = fold_owner.get(old_parent)
                    if owner is not None:
                        owner["children_relinked"] = int(
                            owner.get("children_relinked") or 0) + 1
        # P0-3 ①：edges 的 from/to 随折叠重映射 + 去重（同 from+to+action 合并 count）
        if edges is not None:
            seen: dict[tuple, dict] = {}
            out_edges: list[dict] = []
            for e in edges:
                if not isinstance(e, dict):
                    out_edges.append(e)
                    continue
                e = dict(e)
                e["from"] = keeper_of.get(e.get("from"), e.get("from"))
                e["to"] = keeper_of.get(e.get("to"), e.get("to"))
                if e["from"] == e["to"]:
                    continue  # 折叠把往返边压成自环——丢弃
                key = (e.get("from"), e.get("to"), str(e.get("action")))
                if key in seen:
                    seen[key]["count"] = int(seen[key].get("count") or 1) + int(e.get("count") or 1)
                else:
                    seen[key] = e
                    out_edges.append(e)
            edges[:] = out_edges
        for key in folded_keys:
            pages_by_key.pop(key, None)
        _recompute_depths(pages_by_key)
    return report


def repair_file(index_path: Path, min_sim: float = DEFAULT_MIN_SIM,
                dry_run: bool = False) -> dict:
    """读图谱文件 → 修复 →（非 dry-run）写回。返回修复报告。"""
    index_path = Path(index_path)
    index = load_index(index_path)
    if not index:
        raise FileNotFoundError(f"图谱不存在或不可读: {index_path}——请先 mat index / mat explore")
    index, report = repair(index, min_sim)
    if not dry_run:
        save_index(index_path, index)
    return report
