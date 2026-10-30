"""arrival 试跑验证：图谱记下的到达路径，试跑 N 轮才可信。

盲区：explore 记下 depth/parent_key 就"当数了"，从没验证过这条
路径**是否稳定可复现**——一条偶发路径会被当成可靠复现路径写进报告。

做法：对图谱里每条父链（L0 → … → 目标页），冷启动后**按 parent_action 文案 /
parent_rid 资源 id 逐跳重放**（icon-only 控件没有文案，rid 是它唯一可重放语义），
每跳核对落点 page_key 是否与图谱一致；连跑 trials 轮，通过 ≥ trials-1 轮 → `stable`，
否则 `unstable`（字段 rounds/passed/success_rate/status/reason）。

成本是真机时间（无 AI 成本）；点击预算 `click_budget` 兜底，防长链 × 多轮把设备绑死。
与 explore 的关系：explore 负责"发现路径"，本模块负责"证明路径"——互不调用，
只通过图谱文件（artifacts/page_index/<pkg>.json）交接。
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from .page_index import (
    BACK_HOP,
    digest_page,
    identities_from_controls,
    is_ad_landing,
    is_content_label,
    is_transient_frame,
    keyed_pages,
    load_index,
    page_key,
    pages_equivalent,
    root_reachable,
    save_index,
)
from .safety import is_splash_skip_label
from .ui_tree import parse_nodes

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

_MAX_PATH_LEN = 6  # 超过 6 跳的链路重放性价比太低（每跳都是真机时间 + 失败放大）

#: 碎片互证的 rid 集合 Jaccard 门槛（与 pages_equivalent 同挡位）。
#: 语料实测（test_page_identity_corpus）：同页碎片 rid-only 中位 0.81、跨页 0.02
#: ——0.5 在真碎片与模板复用页对之间留了余量；互证场景还有「同链 + activity 全同
#: + 多轮」三重约束兜底，比全图任意两页的先验强得多。
FRAGMENT_RID_SIM = 0.5


def _rid_set(ids) -> frozenset[str]:
    """identities 里的结构性信号（rid: 前缀）——内容轮换时不变的那部分。"""
    return frozenset(s for s in ids or () if str(s).startswith("rid:"))


def _fragments_aligned(evidences: list[dict], exp_rids: frozenset[str] = frozenset()
                       ) -> tuple[bool, float]:
    """各轮落点证据是否互为**同功能页碎片**：activity 全同 + rid 集两两 J≥门槛。

    传 `exp_rids`（图谱预期页的 rid 集）时还要求**每个落点都与预期页结构对齐**
    ——只比「轮与轮之间」会把「稳定落在同一个错误页」也互证成 stable
    （back 重放落错页的实测教训）。返回 (是否互证, 最小两两 Jaccard)。
    rid 集为空的证据不参与（算不出结构）；任一 activity 缺失也不互证
    （"不知道" ≠ 一致）。
    """
    acts = [e.get("activity") or "" for e in evidences]
    if not acts or any(not a for a in acts) or len(set(acts)) != 1:
        return False, 0.0
    rid_sets = [_rid_set(e.get("ids")) for e in evidences]
    if any(not r for r in rid_sets):
        return False, 0.0
    if exp_rids:
        for r in rid_sets:
            union = r | exp_rids
            if not union or len(r & exp_rids) / len(union) < FRAGMENT_RID_SIM:
                return False, 0.0
    worst = 1.0
    for i in range(len(rid_sets)):
        for j in range(i + 1, len(rid_sets)):
            union = rid_sets[i] | rid_sets[j]
            sim = len(rid_sets[i] & rid_sets[j]) / len(union) if union else 0.0
            if sim < worst:
                worst = sim
    return worst >= FRAGMENT_RID_SIM, worst


def _hop_drift_pass(drv, package: str, landed_xml: str, hop: dict,
                    pages_by_key: dict | None) -> bool:
    """**途经碎片等价**（在线版）：中间跳落点是预期页的 rid 结构等价碎片 ⇒ 放行。

    实测（2026-10-17 真机，tx-news L2–L4 链 0/3）：途经页也是内容轮换页——
    同一入口控件每次点击的落点 page_key 都不同。`pages_equivalent` 需要 landed
    在图谱里才能拿 identities（轮换快照不在）⇒ 放不了行，链死在中间跳。
    在线算 landed 的 rid 集与图谱预期页的 rid 子集比结构等价——内容轮换时
    rid（工程师命名结构）不变、text（内容）变，全 identities Jaccard 会被
    text 稀释放不了行（语料实测）。

    风险控制：rid-only 全图任意两页误判 6.3%（模板复用页对，语料实测），但
    途经场景先验强（同一入口控件的落点）+ activity 双方有值必须一致 +
    阈值同 FRAGMENT_RID_SIM 挡位——宁可漏放行（判落点不符），不可错放行。
    """
    expected = (pages_by_key or {}).get(hop["expected_key"]) if pages_by_key else None
    if not expected:
        return False                 # 预期页不在图谱：没有比对基准，不放行
    exp_act = (expected.get("activity") or "").strip()
    now_act = _foreground_activity(drv)
    if exp_act and now_act and exp_act != now_act:
        return False                 # activity 漂移：真跳到了别的页
    try:
        landed_rids = _rid_set(identities_from_controls(
            digest_page(landed_xml, package)["controls"]))
    except Exception:                # noqa: BLE001 - 算不出结构就别放行
        return False
    exp_rids = _rid_set(expected.get("identities"))
    if not landed_rids or not exp_rids:
        return False                 # "不知道" ≠ 等价，不放行
    union = landed_rids | exp_rids
    return len(landed_rids & exp_rids) / len(union) >= FRAGMENT_RID_SIM


def _chain_of(target: dict, pages_by_key: dict[str, dict]) -> list[dict] | None:
    """目标页的父链 → 重放序列 [{expected_key, action, rid}]（L0 不含，目标页含）。

    链上有缺环（parent 页不在图谱 / 中途缺 parent_action 与 parent_rid）→ None（无法重放）。
    """
    chain: list[dict] = []
    seen: set[str] = set()
    cur: dict | None = target
    while cur is not None and cur.get("parent_key"):
        if cur["page_key"] in seen:
            return None  # 环（理论不该出现，防御）
        seen.add(cur["page_key"])
        # **广告落地页不能当途经节点**（2026-10-23）：它不是被测 App 的功能页，
        # 点进去是信息流广告的随机落地页 —— 重放时「再点一次那条广告」不可复现。
        # 不在这里拦的后果：`graph-repair` 会把它当 donor，借出「点 天猫双11热卖中」
        # 这种链，把不可重放的路径标成"已修复"（实测 tx-news 4 条借链里 3 条走广告）。
        if is_ad_landing(cur):
            return None
        action = cur.get("parent_action") or ""
        rid = cur.get("parent_rid") or ""
        if not action and not rid:
            return None
        chain.append({"expected_key": cur["page_key"], "action": action, "rid": rid})
        cur = pages_by_key.get(cur["parent_key"])
    if not chain or cur is None:
        return None
    chain.reverse()
    return chain


def chain_weak_hop(chain: list[dict]) -> dict | None:
    """链上第一个「内容型文案跳」（无 rid 长文案）——重放必挂，零成本预判。

    verify_arrivals / arrival_provision 共用（2026-10-17 真机：tx-news 8 条链
    24 轮 0 通过、clicks=2，全死于第 1 跳内容文案轮换——「MORIN 法式撞色…」
    这类推荐流标题，重放时早已不是那一条内容）。判据与 hop_replayable 同源。
    """
    return next((h for h in chain if not h.get("rid")
                 and is_content_label(h.get("action") or "")), None)


def _find_clickable(xml: str, package: str, label: str, rid: str = ""):
    """按到达动作找可点控件：资源 id 全等 → 文案全等 → 文案包含。

    rid 是工程师命名、比文案稳定（icon-only 控件根本没有文案），故带 rid 的跳
    先按 rid 匹配；rid 失配（App 改版改名）自然落到文案通道，两通道互补。
    """
    candidates = [n for n in parse_nodes(xml)
                  if n.clickable and n.in_package(package)]
    if rid:
        for node in candidates:
            if node.rid == rid:
                return node
    if not label:
        return None
    labeled = [n for n in candidates if n.label]
    for node in labeled:
        if node.label == label:
            return node
    for node in labeled:
        if label in node.label:
            return node
    return None


def _foreground(drv) -> str:
    try:
        return drv.foreground_app().get("package") or ""
    except Exception:
        return ""


def _foreground_activity(drv) -> str:
    try:
        return (drv.foreground_app().get("activity") or "").strip()
    except Exception:
        return ""


def _probe_launch_activity(drv, package: str, sleep_scale: float) -> str:
    """冷启动一次并记录 **有效首屏** activity（图谱 `launch_activity` 的来源）。

    根可达性判据的基准事实：根 activity ≠ launch 落点 ⇒ 该根下所有链结构性
    不可重放（实测 tx-news：245 条链挂遗留播客根，真机 0/20 全在根核对处拦下，
    且静态借链修不了——donor 池里可达根的完整链是 0）。
    ⚠️ 先**翻掉开屏帧再取**（`_dismiss_splash`）：开屏广告是环境噪声，有效首屏
    是它翻过后的那一帧——与 explore 记录口径一致，根提升判据（提升页 activity
    必须 = 有效首屏）也依赖这一致性。取不到 activity 就返回空串（"不知道" ≠
    不可达，判据不表态）。
    """
    try:
        drv.app_stop(package)
        time.sleep(1.0 * sleep_scale)
    except Exception:            # noqa: BLE001 - 桩未实现 app_stop，退化为 app_start
        pass
    try:
        drv.app_start(package)
        time.sleep(2.5 * sleep_scale)
    except Exception:
        return ""
    _dismiss_splash(drv, package, sleep_scale, {"clicks": 0})
    return _foreground_activity(drv)


def _root_page_of(page: dict, pages_by_key: dict[str, dict]) -> dict | None:
    """父链走到底的根页（launch 的应落点）。链完整性由 `_chain_of` 单独判。"""
    cur: dict | None = page
    seen: set[str] = set()
    while cur is not None and cur.get("parent_key"):
        if cur["page_key"] in seen:
            return None
        seen.add(cur["page_key"])
        cur = pages_by_key.get(cur["parent_key"])
    return cur


def _walk_chain(drv, package: str, chain: list[dict], sleep_scale: float,
                state: dict, pages_by_key: dict[str, dict] | None = None,
                root: dict | None = None,
                evidence: dict | None = None) -> tuple[bool, str]:
    """按父链逐跳点击 + 每跳落点核对（**不含冷启动**）。

    验证（`_replay_once`：冷启动后走链）与执行期前置置备（`arrival_provision`：
    已在 App 内走到目标页）共用同一实现——落点判据只有一份，不会漂移。
    传入 `pages_by_key` 时落点按「同逻辑页碎片等价」核对（`pages_equivalent`）：
    内容轮换/弹窗变体产生的碎片 page_key 不同但属同一逻辑页，精确相等会把
    「实际已到达」误判为落点不符；不传时保持精确相等（旧调用方/单测语义不变）。

    传入 `root`（链根页 dict）时**先核对 launch 落点**：根 activity 与当前前台
    activity 不同 ⇒ 此链从 launch 根本不可达（根页错位），如实报「根页不符」，
    不再让它表现为"第 1 跳未找到可点控件"（真机排障时误导排查方向，2026-10-16）。
    一边取不到 activity 就不表态（"不知道" ≠ "不符"），照旧走链。
    返回 (是否走完, 失败原因)；成功不返回原因。
    """
    if root is not None:
        root_act = (root.get("activity") or "").strip()
        now_act = _foreground_activity(drv)
        if root_act and now_act and root_act != now_act:
            return False, (f"根页不符：图谱根 activity {root_act}，当前 {now_act}"
                           "——launch 落点不是建图根页，此链不可达")
    for i, hop in enumerate(chain):
        current = _foreground(drv)
        if current and current != package:
            return False, f"第 {i + 1} 跳离场（{current[:40]}）"
        try:
            xml = drv.dump()
        except Exception as exc:
            return False, f"第 {i + 1} 跳控件树获取失败: {exc}"
        node = None if hop.get("action") == BACK_HOP and not hop.get("rid") else \
            _find_clickable(xml, package, hop.get("action") or "",
                            hop.get("rid") or "")
        if node is None and hop.get("action") != BACK_HOP:
            want = "、".join(t for t in (
                f"文案「{hop.get('action')}」" if hop.get("action") else "",
                f"资源id「{hop.get('rid')}」" if hop.get("rid") else "") if t)
            return False, f"第 {i + 1} 跳未找到可点控件（{want}）"
        if node is not None:
            match = _BOUNDS_RE.match(node.bounds or "")
            if not match:
                return False, f"第 {i + 1} 跳控件 bounds 异常"
            x1, y1, x2, y2 = (int(g) for g in match.groups())
            try:
                drv.click((x1 + x2) // 2, (y1 + y2) // 2)
            except Exception as exc:
                return False, f"第 {i + 1} 跳点击失败: {exc}"
        else:
            # BACK_HOP（系统 back 到达链，explore._record 记录）：press back 重放——
            # back 是确定性动作，落点核对在下一帧照常把关（回错页如实判不符）
            try:
                drv.press("back")
            except Exception as exc:
                return False, f"第 {i + 1} 跳 back 失败: {exc}"
        state["clicks"] = state.get("clicks", 0) + 1
        time.sleep(1.5 * sleep_scale)
        try:
            landed_xml = drv.dump()
            landed = page_key(landed_xml, package)
        except Exception as exc:
            return False, f"第 {i + 1} 跳落点确认失败: {exc}"
        if landed != hop["expected_key"] and not (
                pages_by_key is not None
                and pages_equivalent(pages_by_key.get(landed) or {},
                                     pages_by_key.get(hop["expected_key"]) or {})):
            # **途经碎片等价**（在线，仅中间跳）：落点是预期页的 rid 结构等价碎片
            # → 放行（内容轮换快照不在图谱，pages_equivalent 放不了行）。
            # 终点跳不单轮放行——错放直接误判 stable，证据必须更强（多轮互证）：
            # 路过页对错不致命（链继续走，终点才见真章），途经先验（同一入口控件
            # 的落点）+ activity 一致已足够。
            if i < len(chain) - 1 and _hop_drift_pass(
                    drv, package, landed_xml, hop, pages_by_key):
                continue
            if evidence is not None and i == len(chain) - 1:
                # **终点落点不符**（链本身走完了）：留证据供 verify_arrivals 做
                # 「多轮碎片互证」——内容轮换页（频道/列表）每次快照的 page_key
                # 都不同，单轮判不符是诚实，多轮互证才能区分「稳定到达同功能页」
                # 与「链在乱跳」。中间跳不符 = 链走偏，不留证据（真失败）。
                try:
                    ids = identities_from_controls(
                        digest_page(landed_xml, package)["controls"])
                except Exception:        # noqa: BLE001 - 证据算不出就空着
                    ids = frozenset()
                evidence.update({"landed_key": landed,
                                 "activity": _foreground_activity(drv),
                                 "ids": sorted(ids)})
            return False, f"第 {i + 1} 跳落点不符（期望 {hop['expected_key'][:8]}，实到 {landed[:8]}）"
    return True, ""


def _dismiss_splash(drv, package: str, sleep_scale: float, state: dict,
                    max_wait: float = 6.0) -> None:
    """launch 后把瞬态启动帧（开屏广告）翻掉——**环境前置**，不计入导航链。

    tx-news 冷启动必经开屏帧（含「跳过」，与首页同 activity）：不翻掉的话走链
    第①跳在广告帧上找控件必死（实测 0/15）。有「跳过」按钮就点（确定性），
    纯倒计时广告就等；当前帧不再命中瞬态帧判据（或超时）即认为已过——
    与图谱侧的根提升（graph_repair._promote_splash_children）配套：根上移到
    真实首页后，重放从「翻掉 splash 后的首页」起链。
    """
    deadline = time.monotonic() + max_wait * sleep_scale
    while time.monotonic() < deadline:
        try:
            xml = drv.dump()
        except Exception:            # noqa: BLE001 - dump 不了就当没有 splash
            return
        labels = [n.label for n in parse_nodes(xml)
                  if n.in_package(package) and n.label]
        if not any(is_splash_skip_label(t) for t in labels):
            return                   # 已过开屏帧
        try:
            for n in parse_nodes(xml):
                if n.clickable and n.in_package(package) \
                        and is_splash_skip_label(n.label):
                    m = _BOUNDS_RE.match(n.bounds or "")
                    if not m:
                        continue
                    x1, y1, x2, y2 = (int(g) for g in m.groups())
                    drv.click((x1 + x2) // 2, (y1 + y2) // 2)
                    state["clicks"] = state.get("clicks", 0) + 1   # 计入预算（诚实）
                    time.sleep(1.2 * sleep_scale)
                    break
            else:
                time.sleep(0.8 * sleep_scale)   # 无按钮：纯倒计时，等它自己翻
        except Exception:            # noqa: BLE001 - 点不动就靠等待翻页
            return


def _replay_once(drv, package: str, chain: list[dict], sleep_scale: float,
                 state: dict, pages_by_key: dict[str, dict] | None = None,
                 root: dict | None = None,
                 evidence: dict | None = None) -> tuple[bool, str]:
    """重放一轮（冷启动 + 逐跳走链）。返回 (是否到达, 失败原因)；成功不返回原因。"""
    # **先停再起**：`app_start` 对后台任务是热恢复（顶部 activity 原样回来），
    # 遗留页会被当成 launch 落点——与 explore 的根锚定同口径（2026-10-16）。
    try:
        drv.app_stop(package)
        time.sleep(1.0 * sleep_scale)
    except Exception:            # noqa: BLE001 - 桩未实现 app_stop，退化为旧行为
        pass
    try:
        drv.app_start(package)
        time.sleep(2.0 * sleep_scale)
    except Exception as exc:
        return False, f"冷启动失败: {exc}"
    _dismiss_splash(drv, package, sleep_scale, state)   # 开屏帧是环境噪声，先翻掉再走链
    return _walk_chain(drv, package, chain, sleep_scale, state, pages_by_key, root,
                       evidence)


def verify_arrivals(drv, package: str, index_path: Path, trials: int = 3,
                    max_paths: int = 8, click_budget: int = 60,
                    sleep_scale: float = 1.0, on_progress=None,
                    page_keys: set[str] | None = None) -> dict:
    """试跑图谱中的到达路径并回写 `arrival` 字段。

    候选页：depth ≥ 1 且父链完整可重放；按深度升序（浅链先验，性价比高）。
    传入 `page_keys` 时只验证这个集合（定向 cohort——如 icon-only 专项只验
    死文案通道+rid 的链；缺省 None = 全图谱浅链优先）。
    通过门槛：passed ≥ trials-1。
    链根为瞬态启动帧（`is_transient_frame`）或 **launch 不可达**（`root_reachable`：
    根 activity ≠ 冷启动落点 `launch_activity`）的页**直接 skip 不烧真机**——
    试跑必然 0%，记录诚实原因即可。图谱缺 `launch_activity` 时先冷启动探测一次
    并回写图谱（此后所有验证/修复共用这一基准事实）。
    """
    trials = max(2, trials)
    index_path = Path(index_path)
    index = load_index(index_path)
    if not index:
        raise FileNotFoundError(f"图谱不存在或不可读: {index_path}——请先 mat index / mat explore")

    # C1 同源修正（2026-10-12 评审）：唤醒走 BaseDriver 契约 `wake_and_unlock()`。
    # 此前 `raw is None ⇒ 跳过` 的判据在 raw 从真实驱动删除后恒成立，本模块的
    # 唤醒同样从未生效。
    try:
        drv.wake_and_unlock()
    except Exception:
        pass  # 桩未实现契约（单测）/真机唤醒失败，都不阻塞校验

    # launch 落点基准：图谱没记录就冷启动探测一次（一次成本，换来全部不可达根
    # 链的零成本诚实 skip——否则每条链要烧 3 轮冷启动才在根核对处失败）。
    # ⚠️ 只在「缺记录」时重探——探测口径（翻开屏后取值）本身正确，已记录的
    # 值是真值（tx-news 实证：重探两次都是 .activity.SplashActivity，它就是
    # 首页 activity），凭命名词表推翻已记录基准只会白烧冷启动且回写值不变。
    launch_act = (index.get("launch_activity") or "").strip()
    if not launch_act:
        launch_act = _probe_launch_activity(drv, package, sleep_scale)
        if launch_act:
            index["launch_activity"] = launch_act

    # ⚠️ 用 `keyed_pages`（会解析 `aliases`）：碎片合并删掉的旧 key 必须仍能反查到代表页，
    # 否则"落点 == 某个被删碎片"时 `pages_equivalent` 恒 False ⇒ 误判没到目标页。
    pages_by_key = keyed_pages(index)
    # targets 只取**活页**：`pages_by_key` 含 alias 后 `values()` 会有重复代表页。
    targets = [p for p in index.get("pages", [])
               if isinstance(p, dict) and p.get("page_key")
               and isinstance(p.get("depth"), int) and p["depth"] >= 1]
    if page_keys is not None:
        wanted = set(page_keys)
        targets = [p for p in targets if p["page_key"] in wanted]
    targets.sort(key=lambda p: (p["depth"], p.get("first_seen") or ""))

    results: list[dict] = []
    clicks = 0
    verified_count = 0
    stopped_early = False
    for page in targets:
        if verified_count >= max_paths:
            break
        if clicks >= click_budget:
            stopped_early = True
            break
        chain = _chain_of(page, pages_by_key)
        if not chain or len(chain) > _MAX_PATH_LEN:
            results.append({"page_key": page["page_key"], "depth": page["depth"],
                            "status": "skipped", "reason": "父链不完整" if not chain
                            else f"链长 {len(chain)} 跳超过 {_MAX_PATH_LEN} 上限"})
            continue
        root = _root_page_of(page, pages_by_key)
        # 瞬态启动帧根（splash 含「跳过」）直接 skip：launch 后该帧被自动翻过，
        # 第 1 跳是对「帧还活着」的竞态——真机试跑只会烧预算换回 0%（实测
        # tx-news 5 条链 0/15），by-design 不可稳定复现，不如如实记录。
        if root is not None and is_transient_frame(root):
            results.append({"page_key": page["page_key"], "depth": page["depth"],
                            "status": "skipped",
                            "reason": "根为瞬态启动帧（含「跳过」类控件）——launch 后"
                                      "该帧被 App 自动翻过，第 1 跳是竞态，"
                                      "by-design 不可稳定复现"})
            continue
        # launch 不可达根（根 activity ≠ 冷启动落点，实测：遗留播客页根）同理
        # 直接 skip——根核对本来也会拦，但那要烧 3 轮冷启动；提前判零成本。
        if root is not None and not root_reachable(root, launch_act):
            results.append({"page_key": page["page_key"], "depth": page["depth"],
                            "status": "skipped",
                            "reason": f"根 launch 不可达：图谱根 activity "
                                      f"{(root.get('activity') or '').strip() or '未知'}，"
                                      f"冷启动落点 {launch_act}——此根下链结构性不可重放，"
                                      f"需从 launch 落点重探入口"})
            continue
        # 内容型文案跳（2026-10-17 真机）：无 rid 的长文案（推荐流标题/榜单名）
        # 随内容轮换，重放按文案匹配必挂——tx-news 8 条链 24 轮 0 通过、clicks=2。
        # 链上任一跳命中 ⇒ 零成本诚实 skip，真机预算让给有 rid 通道的链。
        weak = chain_weak_hop(chain)
        if weak:
            results.append({"page_key": page["page_key"], "depth": page["depth"],
                            "status": "skipped",
                            "reason": f"内容型文案跳：「{(weak.get('action') or '')[:16]}」"
                                      f"（无 rid，随内容轮换）——重放必挂，不烧真机"})
            continue

        passed = 0
        rounds_done = 0
        last_reason = ""
        drift_evidences: list[dict] = []     # 各轮「走完链但终点落点不符」的证据
        for _ in range(trials):
            if clicks >= click_budget:
                stopped_early = True
                break
            state = {"clicks": clicks}
            ev: dict = {}
            ok, reason = _replay_once(drv, package, chain, sleep_scale, state,
                                      pages_by_key, root=root, evidence=ev)
            clicks = state.get("clicks", clicks)
            rounds_done += 1
            if ok:
                passed += 1
            else:
                last_reason = reason
                if ev:
                    drift_evidences.append(ev)

        # **多轮碎片互证**（内容轮换闭环，2026-10-17 真机实测 btn_listener 链）：
        # 内容轮换页（频道/列表）每次快照 page_key 都不同 ⇒ 单轮「落点不符」是
        # 诚实失败，但它无法区分「稳定到达同功能页」与「链在乱跳」。判据：
        # **所有**失败轮都走完了链（终点不符才有证据）且各轮落点 activity 全同、
        # rid 集合两两 Jaccard ≥ 阈值 ⇒ 到达是稳定的，只是内容快照轮换——
        # 这些轮计入 passed（图谱 key 本就是建图时刻的内容快照，见 page_index）。
        # 不写图谱、不放宽单轮判据：证据不足（只 1 轮 / 互不为碎片 / activity 漂移）
        # 时维持 unstable，宁可漏判不可错判。
        if drift_evidences and passed < trials - 1 \
                and len(drift_evidences) == rounds_done:
            exp_rids = _rid_set((pages_by_key.get(page["page_key"]) or {})
                                .get("identities"))
            aligned, j = _fragments_aligned(drift_evidences, exp_rids)
            if aligned:
                passed = rounds_done
                last_reason = (f"落点为同页内容轮换碎片（{len(drift_evidences)}"
                               f"/{rounds_done} 轮互证，rid Jaccard {j:.2f}，"
                               f"activity 一致）——到达稳定，图谱 key 是建图快照")

        entry = {
            "rounds": rounds_done,
            "passed": passed,
            "success_rate": round(passed / rounds_done, 3) if rounds_done else 0.0,
            "status": ("stable" if passed >= trials - 1 else "unstable") if rounds_done else "unverified",
            "reason": "" if not rounds_done else (
                f"试跑 {rounds_done} 轮通过 {passed} 轮，"
                + ("达到" if passed >= trials - 1 else "未达") + f"「通过 ≥{trials - 1}」门槛"
                + (f"；末次: {last_reason}" if last_reason else "")),
            "entry": [{"action": "launch", "target": "App冷启动"}]
            + ([{"action": "back", "target": ""}
                if hop.get("action") == BACK_HOP and not hop.get("rid")
                else {"action": "tap",
                      "target": hop.get("action") or hop.get("rid") or ""}
                for hop in chain]),
            "last_verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _update_page_field(index, page["page_key"], "arrival", entry)
        results.append({"page_key": page["page_key"], "depth": page["depth"],
                        "rounds": rounds_done, "passed": passed,
                        "status": entry["status"], "reason": entry["reason"]})
        verified_count += 1
        if on_progress:
            on_progress(verified_count, max_paths)

    save_index(index_path, index)
    return {"package": package, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "trials": trials, "clicks": clicks, "stopped_early": stopped_early,
            "verified": sum(1 for r in results if r.get("status") in ("stable", "unstable")),
            "stable": sum(1 for r in results if r.get("status") == "stable"),
            "results": results}


def _update_page_field(index: dict, page_key: str, field: str, value) -> None:
    """回写单页字段（复用 update_page 语义：不累加 hits、不碰 structure）。"""
    for page in index.get("pages", []):
        if isinstance(page, dict) and page.get("page_key") == page_key:
            page[field] = value
            break
    index["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def save_report(report: dict, artifacts_dir: Path) -> Path:
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out = artifacts_dir / "arrival.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
