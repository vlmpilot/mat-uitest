"""执行期前置置备：状态不符 → 用图谱父链自动到达（「求解器」最小实现）。

## 问题（真实跑测数据）

失败归因里 **A8「从中间状态开始且无前置条件」13 条 + 「状态不符」10 条 ≈ 23 条**
（41 条失败的 56%，两者可能重叠需去重）：用例冷启动后直接去点深层页面的控件
⇒ 跑出一批**假的定位失败**（人去查定位链，而问题根本不在那儿）。
声明路径帮不上忙：`cases/` 33 份里 **0 份**带 `preconditions` 字段
（存量用例几乎没写声明前置），`precondition_expand`（LLM 展开）只覆盖导入侧的声明前置。

## 做法（零 AI 成本，零件全是现成的）

执行到**首个非 launch 步骤**前（runner 钩子，每轮一次）：

1. **推断目标页**：首个带 `target` 的步骤，其定位值去图谱控件清单里对表——
   所有命中页同属**一个 activity** 才算数（实测同页稳定性仅 12%，
   同一逻辑页常碎片化成多个 page_key，按 page_key 判唯一对真实图谱过严；
   跨 activity = 真歧义，**不猜**；定位失败 ⇒ 原行为照旧）；
2. **状态核对**：`page_key(dump)` 已在目标页 ⇒ 跳过（这正是「状态不符」的另一半：
   用例声明的前提成立时什么都不做）；
3. **置备**：复用 `arrivals._walk_chain`（验证与置备**共用同一落点判据**，不会漂移）
   按父链逐跳点击（含 BACK_HOP 系统返回跳）；冷启动与 `_replay_once` 全同口径
   （先停再起 + 翻掉开屏帧），且当前页不是链根时也冷启动重锚——链从根起走；
4. **根预判**：瞬态启动帧根 / launch 不可达根（`root_reachable`，与 verify_arrivals
   同判据）直接放弃——注定失败的链不烧真机预算；
5. **终点单轮碎片等价**：内容轮换页走完链但落点快照不在图谱时，落点 activity 与
   目标页一致 + rid 结构对齐（J≥0.5）即视为到达（置备不写图谱，比验证侧多轮
   互证的放行条件更严，错放代价 = 用例原样照跑）；
6. **证据**：写进 `RunResult.arrival`（run.json / 报告可见）——置备了几跳、成败、原因，
   让「这条用例被自动带到过某页」**可见可审**，而不是黑箱替人点了两下。

## 边界（刻意收窄）

- 只做**页面级**到达。数据态（「存在至少 1 条历史搜索」）、系统态（权限）、
  同页内的状态（tab 选中态）置备不了——动作集里没有能改它们的原语；
- 定位值在多个页出现 ⇒ 放弃（宁可不动，不可走错页点错东西）；
- 链长超 `arrivals._MAX_PATH_LEN`（6 跳）或父链缺环 ⇒ 放弃（与 arrival 验证同门槛：
  验证都不敢走的链，置备更不该走）；
- 点击预算兜底（防长链把设备绑死），超预算即停并留因；
- 全程吞错降级：置备失败 = 用例照原样跑。⚠️ **"原样"必须真的回到 launch 后的那一屏**：
  置备自己点了两下、落在错页时，光"继续"会把用例丢在详情页上（实测 xhs-5/xhs-6 3/3 复现）
  ⇒ 失败后**重放一次冷启动**（`evidence["restored"]`），恢复不了才退回旧行为；
- `MAT_AUTO_ARRIVE=0` 可整体关闭。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .page_index import (
    BACK_HOP,
    is_transient_frame,
    load_index,
    page_key,
    pages_equivalent,
    root_reachable,
)

__all__ = ["auto_arrive_enabled", "infer_target_page", "provision"]

#: 置备的点击预算：比 `arrivals` 验证的 60 小得多——置备每轮只发生一次，
#: 超过这个数说明链路本身不可靠，继续点只是烧真机时间。
_CLICK_BUDGET = 12

#: 与 `arrivals` 同门槛：超过 6 跳的链置备性价比太低。
_MAX_PATH_LEN = 6

#: 推断目标页时**可对照图谱**的定位策略（其余策略如 xpath/point/image 无法与控件清单对表）。
_MATCHABLE = {"text", "textContains", "description", "res-id", "id"}


def auto_arrive_enabled() -> bool:
    """执行期自动置备开关（默认开）。`MAT_AUTO_ARRIVE=0` 关闭。"""
    return os.environ.get("MAT_AUTO_ARRIVE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _locator_values(locator) -> list[tuple[str, str]]:
    """收集定位链上所有 (strategy, value)（fallback 链逐级降级，都要对表）。"""
    out: list[tuple[str, str]] = []
    cur = locator
    while cur is not None:
        out.append((str(cur.strategy), str(cur.value or "")))
        cur = cur.fallback
    return out


def _page_hit(page: dict, strategy: str, value: str) -> bool:
    """这条定位值是否命中该页控件清单（text/desc 精确对 text/desc；res-id 精确对 res-id）。"""
    for ctl in page.get("controls") or []:
        text = str(ctl.get("text") or ctl.get("desc") or "")
        rid = str(ctl.get("res-id") or "")
        if ((strategy in ("text", "textContains", "description") and text == value)
                or (strategy in ("res-id", "id") and rid == value)):
            return True
    return False


def infer_target_page(case, pages: list[dict]) -> dict | None:
    """从用例首个带 target 的步骤**推断期望起始页**。

    规则（⚠️ 按 **activity 聚类**判歧义，不按 page_key）——实测图谱
    「同页稳定性仅 12%」：同一逻辑页常被碎片化成多个 page_key（弹窗/刷新变体），
    按 page_key 判唯一会对真实图谱全军覆没。改为：
      1. 首个动作步骤定位链上每个可对表的 (strategy, value) 都要能在图谱命中
         （任一值全图谱无命中 ⇒ 推断不出）；
      2. 所有命中页必须同属**一个 activity**（跨 activity = 真歧义 ⇒ 放弃）；
      3. 代表页在命中碎片里选：`arrival.status == "stable"` 优先（路径已被
         `mat arrival` 验证过），其次 `hits` 最多（最常走的形态）。
    返回页 dict；推断不出 ⇒ None（不置备——宁可不动，不可走错）。
    """
    from .dsl import Action
    from .page_index import is_transient_frame

    step = next((s for s in case.steps
                 if s.action != Action.LAUNCH and s.target is not None), None)
    if step is None:
        return None
    # 只信「首个动作步骤」的定位：后续步骤的页面由前置步骤导航，推断它们反而会走错。
    candidates: list[dict] | None = None  # 逐个定位值取交集（控件应同时被各值命中）
    for strategy, value in _locator_values(step.target):
        if not value or strategy not in _MATCHABLE:
            continue
        hits = [p for p in pages if _page_hit(p, strategy, value)]
        # **瞬态帧（开屏广告/闪屏）不能当目标页**：用例第一步常是「点击跳过」，
        # 那是冷启动后立刻要做的一次性动作，不代表"期望从这一页开始"。
        # 把它当目标页的后果（2026-10-21 实测 tx-news 6 条）：该页是瞬态帧、没有父链
        # ⇒ 置备报"父链不完整"直接放弃，用例从当前页继续，第一步找不到「跳过」。
        hits = [p for p in hits if not is_transient_frame(p)]
        if not hits:
            return None  # 可对表的定位值在全图谱都找不到（或只命中瞬态帧）⇒ 推断不出
        acts = {(p.get("activity") or "") for p in hits}
        if len(acts) > 1:
            return None  # 同一定位值跨多个 activity ⇒ 真歧义
        if candidates is None:
            candidates = hits
        else:
            inter = [p for p in candidates if p in hits]
            candidates = inter if inter else hits  # 交集空（fallback 对不上表）⇒ 保留主定位
    if not candidates:
        return None
    # 代表页：arrival 已验证 stable 的优先，其次 hits 最多（同一逻辑页最常走的形态），
    # 再次 **depth 最浅**。
    #
    # ⚠️ `depth` 这一档是 2026-10-29 补的，补之前这两档打平就落到 `page_key` 字母序——
    # 那是个**没有语义的 tiebreaker**。实测现场（xhs 图谱重建后，3/3 稳定复现）：
    # 根页 `bad6f75c`（depth=0）与「运动」频道页 `00848095`（depth=1）**都含 `text=我`**、
    # `hits` 都是 1、都没有 arrival 记录 ⇒ 字母序把 depth=1 的那条排前面 ⇒ 置备去点了
    # 「运动」⇒ 落到笔记详情页 ⇒ 用例的 `tap 我` 在详情页上必然失败，报成 `locate-fail`。
    # 深度更浅 = 到达更便宜、更不可能走错页，是同分时唯一有意义的次序。
    def _rank(p: dict):
        arrival = (p.get("arrival") or {}) if isinstance(p.get("arrival"), dict) else {}
        stable = 1 if arrival.get("status") == "stable" else 0
        depth = p.get("depth")
        return (-stable, -(p.get("hits") or 0), 999 if depth is None else depth,
                str(p.get("page_key") or ""))
    best = sorted(candidates, key=_rank)[0]
    # **等价碎片里取「到达最便宜」的那条记录**（2026-10-23 实测，tx-news）。
    # 图谱会把同一个逻辑页记成多条（首页有 15 条），其中一条可能挂在
    # 「首页 → 广告 → 广告 → back」这样的深链上（实测 depth=3）。按 page_key 选到那条，
    # 置备就会去重放一条 5 跳的垃圾链，报「父链不完整」——而正确答案是
    # **「你已经在首页了，什么都不用做」**。实测 006/007/013 三条的目标页全是这种副本。
    # ⇒ 在与 best 等价的碎片里优先 depth 最浅的（depth 未知的排最后：它没有父链）。
    twins = [p for p in candidates if p is best or pages_equivalent(p, best)]

    def _depth_rank(p: dict):
        depth = p.get("depth")
        return (999 if depth is None else depth, -(p.get("hits") or 0))

    return sorted(twins, key=_depth_rank)[0]


def provision(case, drv, package: str, index_path: Path,
              sleep_scale: float = 1.0, click_budget: int = _CLICK_BUDGET) -> dict:
    """执行期置备：核对当前页 → 推断目标页 → 按父链自动到达。

    **永不抛出**（调用方 runner 已再兜一层）：返回证据 dict——
    `{"needed": bool, "ok": bool|None, "page": str, "hops": int,
      "reason": str, "clicks": int, "entry": list}`；
    `needed=False` 表示无事可做或做不了（reason 说明），行为与从前逐字一致。
    """
    evidence: dict = {"needed": False, "ok": None, "page": "", "hops": 0,
                      "reason": "", "clicks": 0, "entry": [],
                      # 置备失败后有没有把 App 送回 launch 后的状态（2026-10-29 起）
                      "restored": None}
    try:
        index = load_index(Path(index_path))
        pages = [p for p in (index or {}).get("pages", []) if isinstance(p, dict)]
        if not pages:
            return {**evidence, "reason": "图谱为空或不可读"}
        from .arrivals import (
            _chain_of,
            _dismiss_splash,
            _foreground,
            _fragments_aligned,
            _rid_set,
            _root_page_of,
            _walk_chain,
            chain_weak_hop,
        )

        target = infer_target_page(case, pages)
        if target is None:
            return {**evidence, "reason": "推断不出唯一目标页（无定位/歧义/图谱无命中）"}

        key = target.get("page_key") or ""
        evidence["page"] = key[:8]
        pages_by_key = {p.get("page_key"): p for p in pages if p.get("page_key")}
        try:
            current = page_key(drv.dump(), package)
        except Exception as exc:
            return {**evidence, "page": key[:8], "reason": f"当前页识别失败: {exc}"}
        # 已在目标页的判据含**碎片等价**：当前页是同一逻辑页的另一个碎片时无需置备
        if current == key or pages_equivalent(pages_by_key.get(current) or {}, target):
            return {**evidence, "reason": "已在目标页"}
        evidence["needed"] = True  # 从这里起：目标页 ≠ 当前页，确实需要置备

        chain = _chain_of(target, pages_by_key)
        # **目标就是根页（无父页）≠ 断链**：`_chain_of` 对根页返回 None（因为链为空），
        # 旧实现据此报「父链不完整」并**直接放弃**——连冷启动都不做。
        # 后果（2026-10-21 实测）：用例在 `launch` 热恢复出来的**上一个用例遗留页**上开跑
        # （截图可见搜索框还留着上一轮的词），第 1 步就定位失败，而错误信息只说"父链不完整"，
        # 把人引向图谱排查。根页的正确语义是"不需要导航，但必须确认自己在根页"。
        if chain is None and not (target.get("parent_key") or "").strip():
            chain = []
        if chain is None or len(chain) > _MAX_PATH_LEN:
            return {**evidence, "ok": False, "page": key[:8],
                    "reason": ("父链不完整" if chain is None
                               else f"链长 {len(chain)} 跳超过 {_MAX_PATH_LEN} 上限")}
        evidence["hops"] = len(chain)

        # 内容型文案跳预判（与 verify_arrivals 同判据，零成本）：无 rid 的长文案
        # 随内容轮换，重放按文案匹配必挂（tx-news 2026-10-17 实测 8 链 24 轮 0 通过）。
        weak = chain_weak_hop(chain)
        if weak:
            return {**evidence, "ok": False, "page": key[:8],
                    "reason": f"内容型文案跳：「{(weak.get('action') or '')[:16]}」"
                              f"（无 rid，随内容轮换）——重放必挂，置备放弃"}

        # **根预判**（与 verify_arrivals 同判据，零成本——注定失败的链不冷启动不烧预算）：
        # ① 瞬态启动帧根：launch 后该帧被 App 自动翻过，第 1 跳是竞态；
        # ② launch 不可达根（根 activity ≠ 冷启动落点）：此链结构性不可重放。
        # 图谱缺 launch_activity 时不表态（"不知道" ≠ "不可达"），交给 _walk_chain 的
        # 根 activity 核对兜底——置备是单链低频，不值得为预判单独冷启动探测。
        root = _root_page_of(target, pages_by_key)
        if root is not None and is_transient_frame(root):
            return {**evidence, "ok": False,
                    "reason": "根为瞬态启动帧（含「跳过」类控件）——launch 后该帧被 App "
                              "自动翻过，此链 by-design 不可稳定重放，置备放弃"}
        launch_act = (index.get("launch_activity") or "").strip()
        if root is not None and launch_act and not root_reachable(root, launch_act):
            return {**evidence, "ok": False,
                    "reason": f"根 launch 不可达：图谱根 activity "
                              f"{(root.get('activity') or '').strip() or '未知'}，"
                              f"冷启动落点 {launch_act}——此链结构性不可重放，置备放弃"}

        state = {"clicks": 0}
        # 冷启动决策：不在被测包内，**或当前页不是链根**（链从根起走，落在深层页/别处
        # 直接走链第 1 跳必死）——都要冷启动重锚。已在根页（含等价碎片）则省一次冷启动。
        on_root = current == (root or {}).get("page_key") or pages_equivalent(
            pages_by_key.get(current) or {}, root or {})
        cold = _foreground(drv) != package or not on_root
        evidence["entry"] = ([{"action": "launch", "target": "App冷启动"}] if cold else []) \
            + ([{"action": "back", "target": ""}
                if hop.get("action") == BACK_HOP and not hop.get("rid")
                else {"action": "tap",
                      "target": hop.get("action") or hop.get("rid") or ""}
                for hop in chain])

        if cold:
            # 与 arrivals._replay_once 同口径：**先停再起**（app_start 对后台任务是热
            # 恢复，遗留页会被当成 launch 落点）+ 开屏帧翻掉再走链（实测 0/15 的教训）。
            # splash 的「跳过」点击计入 state 预算（诚实）。
            try:
                drv.app_stop(package)
                time.sleep(1.0 * sleep_scale)
            except Exception:        # noqa: BLE001 - 桩未实现 app_stop，退化为 app_start
                pass
            try:
                drv.app_start(package)
                time.sleep(2.0 * sleep_scale)
            except Exception as exc:
                return {**evidence, "ok": False, "reason": f"冷启动失败: {exc}"}
            _dismiss_splash(drv, package, sleep_scale, state)
        if state["clicks"] < click_budget:
            ev: dict = {}
            ok, reason = _walk_chain(drv, package, chain, sleep_scale, state,
                                     pages_by_key, root=root, evidence=ev)
            if not ok and ev.get("ids") and ev.get("activity"):
                # **终点单轮碎片等价**（置备特有，2026-10-17 判据同步）：内容轮换页
                # 走完链但落点快照不在图谱 ⇒ 单轮判不符。验证侧靠多轮互证才放行
                # （错放会误判 stable 写进图谱）；置备不写图谱，且错放代价 = 用例在
                # 原样状态下跑（与不置备持平）——单轮 + **更严约束**即可放行：
                # 落点 activity 与目标页一致 + 落点 rid 集与目标页 rid 对齐
                # （J ≥ 0.5，同 FRAGMENT_RID_SIM 挡位）。证据缺失/目标页无结构
                # 信号时不放行（"不知道" ≠ 等价），维持诚实失败。
                tgt_act = (target.get("activity") or "").strip()
                tgt_rids = _rid_set(target.get("identities"))
                if tgt_act and tgt_rids and ev["activity"] == tgt_act:
                    aligned, j = _fragments_aligned([ev], tgt_rids)
                    if aligned:
                        ok = True
                        reason = (f"落点为同页内容轮换碎片（单轮结构对齐，"
                                  f"rid Jaccard {j:.2f}，activity 一致）——置备目的已达成")
            evidence["ok"] = ok
            evidence["reason"] = reason
            evidence["clicks"] = state.get("clicks", 0)
            if not ok:
                # **走错了就得把人送回去**（2026-10-29）：置备自己点了两下、落到一个
                # 不是目标页的地方，如果就这么把控制权交还用例，"原样继续"已经不是
                # launch 后的那一屏了 —— 用例的 `tap 我` 会在详情页上失败，报成
                # `locate-fail`，人去查定位链。本文件顶部"置备失败不会比从前更糟"
                # 那句，正是被 xhs-5/xhs-6 这个现场推翻的（3/3 稳定）。
                # 恢复动作 = 重放 launch 后的状态（先停再起），失败也不抛。
                try:
                    try:
                        drv.app_stop(package)
                        time.sleep(1.0 * sleep_scale)
                    except Exception:      # noqa: BLE001 - 桩没实现 app_stop：至少把 App 拉回来
                        pass
                    drv.app_start(package)
                    time.sleep(2.0 * sleep_scale)
                    evidence["restored"] = True
                except Exception:          # noqa: BLE001 - 恢复失败也只是回到旧行为
                    evidence["restored"] = False
        else:
            evidence["ok"] = False
            evidence["reason"] = "点击预算已用尽"
        return evidence
    except Exception as exc:  # 置备绝不把用例跑挂
        return {**evidence, "reason": f"置备异常降级: {type(exc).__name__}: {exc}"}
