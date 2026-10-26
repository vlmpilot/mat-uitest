"""页面身份的**离线**回归测试：拿真实设备录制的 UI 树，挡住"读代码看不出来"的回归。

**为什么必须有它**
    `page_key` 的实现读起来是"先把动态文本置空，再对控件语义集合哈希"——**只看代码会判"应该还行"**。
    拿真实树一测：同页稳定性 **0/19**（首页 14 份观测 → 14 个不同 key）。
    这类回归**不可能靠读代码挡住**，只能靠真实样本。本测试不插设备、不调模型、纯 CPU，可进 CI。

**语料**：`benchmarks/page_identity/`（268 份真实 uiautomator 树 / 4 个 App / 54 页，
其中 43 页有多份观测）。由 `scripts/build_page_identity_corpus.py` 从第三方录制产物抽出。
语料**不进公开仓库**（第三方 App 真实 UI 结构），所以在公开仓库里本测试会 skip。

**本测试断言什么**（四类，顺序即重要性）
    1. 语料完整性 —— manifest 的 sha256 与实际文件一致（防手改语料把测试"改绿"）
    2. 现在**能用**的性质不许退化 —— 跨页分离度、包含度分离度（这是未来上聚类的地基）
    3. 现在**坏着**的性质不许更坏 —— `page_key` 稳定性基线约 12%（某 res-id 乱序的超级 App 0/19）；
       跨轮 `tried` 命中率当前 10%。这两条是**地板**不是合格线：挡"越改越差"，
       但**故意不为它们背书**——把它们当达标是最容易犯的错。
    4. **瓶颈归属** —— 坐标重合度 86.5% ⇒ 复用失效是 `page_key` 的锅，不是坐标抖动。
       没有这条，容易治错病（去给坐标加容差）。
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import os
from collections import defaultdict
from pathlib import Path

import pytest

from insta_engine.explore import _candidate, _identities, _same_page, match_prior_page
from insta_engine.page_index import is_degenerate_res_id, page_key
from insta_engine.ui_tree import parse_nodes

CORPUS = Path(__file__).resolve().parents[2] / "benchmarks" / "page_identity"


def _containment(a: frozenset, b: frozenset) -> float:
    """包含度（交集 / 较大集合）——`_same_page` 与认页共用同一口径。"""
    return len(a & b) / max(len(a), len(b)) if a and b else 0.0

#: 语料缺失时是 skip 还是 fail。本地/CI 想强制要求语料在场就设 `MAT_REQUIRE_CORPUS=1`
#: （否则语料被误删时测试会静默变绿——那是这类"外部数据依赖"最典型的失效方式）。
REQUIRE = os.environ.get("MAT_REQUIRE_CORPUS", "").strip() in {"1", "true", "yes"}

#: 跨页误判上限。实测 7/1431 = 0.5%，留余量；一旦某次改动让页面大面积合并，这里会红。
MAX_CROSS_FALSE_MERGE = 0.03

#: 同页包含度中位 - 跨页包含度中位 的最小间隔。实测 0.70 - 0.02 = 0.68。
MIN_CONTAINMENT_GAP = 0.30

#: `page_key` 同页稳定性的**地板**。
#: 实测基线（2026-10-01，268 树 / 54 页 / 43 多观测页）：整体 **12%（5/43）**，其中
#: 实测结论：res-id 稳定的 App 页签名能稳，res-id 逐次乱序的不能（某超级 App 0/19）。
#: ⚠️ **12% 不等于"可用"**：跨轮 `tried` 复用需要远高于此，本测试**故意不为它背书**，
#: 只把它当地板防止继续变坏。真正的修法。
STABILITY_FLOOR = 0.10

#: 跨轮 `tried` 命中率地板。实测 **10.0%（99/987）**——即第 1 轮点过的分支，第 2 轮只有 1/10 会被跳过。
#: ⚠️ 这不是"能用"：增量探索的全部价值在于"跳过已知分支、预算花在新分支"，10% 等于基本没跳。
#: 对照上限：**若 `page_key` 稳定、只比坐标，命中率可达 86.5%** ⇒ 瓶颈在 key，不在坐标。
#: 修好 key 后应把本值抬到 ≥0.7。
MIN_CROSS_ROUND_REUSE = 0.05

#: 坐标重合度地板。实测 87% —— 说明控件位置本身是稳的，所以"复用失效"不能赖坐标抖动。
MIN_BOUNDS_OVERLAP = 0.70

#: **按包含度认页**的跳过率地板。实测 **80.4%**（旧机制 `page_key` 精确匹配只有 **10.0%**）。
#: 这条是 2026-10-01 那个修复的回归门禁：谁把认页弄坏了，这里会红。
MIN_IDENTITY_SKIP = 0.50

#: **错跳率**上限——认错页会静默跳过本页真实分支（丢覆盖），必须单独盯。
#: 实测 **1.7%**（若去掉"明显胜出"间距判据会升到 10%+，所以间距判据不是可选项）。
#: 分母是**第 2 轮的候选总数**，这样"跳过率"与"错跳率"直接可比。
#: 2026-10-16 重校：`_candidate` 开始排除「无文案且无 rid」死链（icon-only 专项），
#: 候选池 1004 → 647、认对 837 → 525（差值是死链控件的假收益）、**绝对错跳 17 → 16
#: 反而降了**——比率 1.7% → 2.5% 纯属分母变纯，匹配本身没退化，阈值按新口径放到 3%。
MAX_IDENTITY_WRONG = 0.03


def _load_corpus() -> tuple[dict, list[dict]]:
    if not (CORPUS / "manifest.json").exists():
        msg = (f"页面身份语料不在：{CORPUS}\n"
               f"（它是从第三方录制产物抽出的评测资产，不进公开仓库；"
               f"用 scripts/build_page_identity_corpus.py 重建）")
        if REQUIRE:
            pytest.fail(msg)
        pytest.skip(msg)
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in
            (CORPUS / "truth.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    return manifest, rows


@pytest.fixture(scope="module")
def measured() -> dict:
    """把语料跑一遍，算出所有指标。模块级缓存——268 份树只解析一次。"""
    manifest, rows = _load_corpus()

    items: list[dict] = []
    for r in rows:
        xml = gzip.open(CORPUS / r["tree"], "rt", encoding="utf-8", errors="ignore").read()
        items.append({
            "app": r["app_id"],
            "page": r["page_name"],
            "key": page_key(xml, r["app_id"]),
            "ids": _identities(xml, r["app_id"]),
            "cands": _candidate(xml, r["app_id"]),
            # 结构信号（rid-only）：`pages_equivalent_layout`（若采用）的判据原料。
            # rid 是工程师命名，模板/框架类控件的 rid 跨内容稳定——轮播轮换、
            # 弹窗变体下「同 activity + rid 结构重合」是否足以认"同类页"靠它回答。
            "struct": {n.rid for n in parse_nodes(xml)
                       if n.rid and n.in_package(r["app_id"])
                       and not is_degenerate_res_id(n.rid)},
        })

    by_page: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for it in items:
        by_page[(it["app"], it["page"])].append(it)

    within: list[float] = []
    for v in by_page.values():
        within += [_containment(a["ids"], b["ids"]) for a, b in itertools.combinations(v, 2)]
    cross: list[float] = []
    for (_, v1), (_, v2) in itertools.combinations(sorted(by_page.items()), 2):
        cross.append(_containment(v1[0]["ids"], v2[0]["ids"]))

    # 结构（rid-only）版同页/跨页分布——评估「结构等价认页」是否可行（2026-10-16）。
    def _jaccard(a: set, b: set) -> float:
        return len(a & b) / len(a | b) if (a or b) else 0.0

    struct_within = [_jaccard(a["struct"], b["struct"])
                     for v in by_page.values()
                     for a, b in itertools.combinations(v, 2)]
    struct_cross = [_jaccard(v1[0]["struct"], v2[0]["struct"])
                    for (_, v1), (_, v2) in itertools.combinations(sorted(by_page.items()), 2)]

    # 跨页误判：不同页名的首份观测被 _same_page(阈值 0.8) 判成"同页"
    firsts = [v[0] for _, v in sorted(by_page.items())]
    pairs = list(itertools.combinations(firsts, 2))
    false_merge = sum(1 for a, b in pairs if _same_page(a["ids"], b["ids"]))

    multi = {k: v for k, v in by_page.items() if len(v) > 1}
    stable_pages = sum(1 for v in multi.values() if len({x["key"] for x in v}) == 1)

    # 分 App 统计：稳定性是**分 App 的**现象（res-id 稳的能稳，乱序的不能）。
    # 只报整体数字会掩盖"某个 App 从 60% 掉到 0%"这种退化。
    per_app: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (app, _), v in multi.items():
        per_app[app][1] += 1
        if len({x["key"] for x in v}) == 1:
            per_app[app][0] += 1

    cands = sorted(len(it["cands"]) for it in items)

    def median(xs: list[float]) -> float:
        s = sorted(xs)
        return s[len(s) // 2] if s else 0.0

    def q(xs: list[float], p: float) -> float:
        s = sorted(xs)
        return s[min(len(s) - 1, int(p * len(s)))] if s else 0.0

    # 跨轮 `tried` 复用预测：模拟"跑两轮"，第 1 轮记下 tried={(page_key, bounds)}，
    # 看第 2 轮能命中多少。这是 那个"增量探索疑已静默失效"的**离线复现**。
    # 同时算"假设 key 已稳定、只比 bounds"的命中率——用来区分瓶颈在 key 还是在坐标。
    xr_total = xr_hit = b_total = b_hit = 0
    # 新机制（2026-10-01）：按**信号包含度 + 明显胜出**认页，再复用那一页的 tried。
    # 用页面名当真值，分别记"认对贡献"与"**认错贡献**"——后者是静默丢覆盖，必须单独盯。
    id_hit_right = id_hit_wrong = 0
    id_skipped = round2_total = 0
    for v in multi.values():
        o1, o2 = v[0], v[1]
        t1 = {(o1["key"], c["bounds"]) for c in o1["cands"]}
        t2 = {(o2["key"], c["bounds"]) for c in o2["cands"]}
        xr_total += len(t1)
        xr_hit += len(t1 & t2)
        b1 = {c["bounds"] for c in o1["cands"]}
        b2 = {c["bounds"] for c in o2["cands"]}
        b_total += len(b1)
        b_hit += len(b1 & b2)
        # 在所有"图谱页"（= 各多观测页的首份观测）里认页。**走线上同一份实现**
        # （`match_prior_page`）——测试里再写一遍匹配逻辑就会两边漂移，那样测的是个不存在的东西。
        prior = [(vv[0]["ids"], {c["bounds"] for c in vv[0]["cands"]})
                 for vv in multi.values()]
        matched = match_prior_page(o2["ids"], prior)
        # 口径（**以第 2 轮的候选为分母**，这样两个数直接可比、不会自欺）：
        #   跳过 = matched ∩ 本轮候选            → 占本轮候选的比例 = 增量收益
        #   错跳 = (matched \ 本页上次试过) ∩ 本轮候选 → 占本轮候选的比例 = **静默丢覆盖**
        # 不能用"匹配集合是否等于本页集合"来判对错——那会把"正确匹配但集合被交集收缩"
        # 也算成认错，把度量本身搞脏（这个坑踩过一次）。
        own_tried = {c["bounds"] for c in multi[(o2["app"], o2["page"])][0]["cands"]}
        skipped = matched & b2
        id_skipped += len(skipped)
        id_hit_right += len(skipped & own_tried)
        id_hit_wrong += len(skipped - own_tried)
        round2_total += len(b2)

    return {
        "manifest": manifest,
        "trees": len(items),
        "pages": len(by_page),
        "multi_pages": len(multi),
        "cross_false_merge": false_merge,
        "cross_pairs": len(pairs),
        "within_median": median(within),
        "cross_median": median(cross),
        "within_pairs": len(within),
        "cross_cmp_pairs": len(cross),
        "struct_within_median": median(struct_within),
        "struct_within_p25": q(struct_within, 0.25),
        "struct_cross_median": median(struct_cross),
        "struct_cross_false": sum(1 for x in struct_cross if x >= 0.5),
        "struct_cross_pairs": len(struct_cross),
        "stable_pages": stable_pages,
        "stability": stable_pages / len(multi) if multi else 0.0,
        "per_app": {k: (v[0], v[1]) for k, v in sorted(per_app.items())},
        "xround_total": xr_total,
        "xround_hit": xr_hit,
        "xround_reuse": xr_hit / xr_total if xr_total else 0.0,
        "bounds_overlap": b_hit / b_total if b_total else 0.0,
        "id_skip_total": round2_total,
        "id_hit_right": id_hit_right,
        "id_hit_wrong": id_hit_wrong,
        "id_skip_rate": id_skipped / round2_total if round2_total else 0.0,
        "id_wrong_rate": id_hit_wrong / round2_total if round2_total else 0.0,
        "cand_median": cands[len(cands) // 2] if cands else 0,
        "cand_zero": sum(1 for c in cands if c == 0),
    }


def _report(m: dict) -> str:
    apps = "，".join(f"{k.split('.')[-1]} {a}/{b}" for k, (a, b) in m["per_app"].items())
    return (f"语料 {m['trees']} 树 / {m['pages']} 页（多观测 {m['multi_pages']}）｜"
            f"page_key 同页稳定性 {m['stable_pages']}/{m['multi_pages']}"
            f"（{m['stability']:.0%}；分 App：{apps}）｜"
            f"跨页误判 {m['cross_false_merge']}/{m['cross_pairs']}"
            f"（{m['cross_false_merge'] / max(1, m['cross_pairs']):.1%}）｜"
            f"包含度中位 同页 {m['within_median']:.2f} vs 跨页 {m['cross_median']:.2f}｜"
            f"跨轮 tried 命中 {m['xround_hit']}/{m['xround_total']}"
            f"（{m['xround_reuse']:.1%}；坐标重合 {m['bounds_overlap']:.0%}）｜"
            f"认页跳过 {m['id_skip_rate']:.1%}（认对 {m['id_hit_right']} / **错跳 {m['id_hit_wrong']}**）｜"
            f"候选中位 {m['cand_median']}")


def test_corpus_is_intact(measured: dict) -> None:
    """语料必须没被手改：manifest 里每份树的 sha256 都要对得上。

    没有这条，任何人改几份树就能把下面的断言"调绿"——那护栏就变成摆设了。
    """
    bad = []
    for f in measured["manifest"]["files"]:
        raw = (CORPUS / f["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != f["sha256"]:
            bad.append(f["path"])
    assert not bad, f"语料被改动过（{len(bad)} 份，前 3：{bad[:3]}）——请重跑构建脚本，不要手改"


def test_cross_page_separation_not_regressed(measured: dict) -> None:
    """不同页面不许被大面积判成同一页。

    这是当前**已经在工作**的性质（实测 2.2%），也是所有下游（图谱去重、探索增量）的地基。
    一旦 `_identities` 变得不具判别力（例如误把通用底栏文案收进来），这里会立刻红。
    """
    rate = measured["cross_false_merge"] / max(1, measured["cross_pairs"])
    assert rate <= MAX_CROSS_FALSE_MERGE, (
        f"跨页误判率 {rate:.1%} > 上限 {MAX_CROSS_FALSE_MERGE:.0%}\n{_report(measured)}\n"
        f"→ `_identities` 的判别力退化了（检查是不是收进了跨页通用文案）")


def test_containment_separation_holds(measured: dict) -> None:
    """同页包含度必须明显高于跨页——这是"用包含度替掉精确哈希"这条路的地基。

    实测 0.63 vs 0.02。这条断言的意义：将来真上聚类时，地基还在不在。
    """
    gap = measured["within_median"] - measured["cross_median"]
    assert gap >= MIN_CONTAINMENT_GAP, (
        f"同页/跨页包含度间隔只有 {gap:.2f}（要求 ≥{MIN_CONTAINMENT_GAP}）\n{_report(measured)}\n"
        f"→ 包含度不再能区分同页与跨页，聚类方案的前提没了")


def test_page_key_stability_does_not_regress(measured: dict) -> None:
    """`page_key` 的跨观测稳定性**不许再变坏**——但本测试**不宣称它可用**。

    实测基线约 12%（5/43）：res-id 稳定的 App 页签名能稳，逐次乱序的不能。
    跨轮 `tried` 复用要的是"绝大多数页都稳"，12% 远远不够。

    所以这条断言的定位是**地板**，不是合格线：
      - 它挡住"越改越差"（例如 `_stable_text` 被削弱、res-id 被更多地纳入哈希）；
      - 它**故意不**为 12% 背书——把地板设成"实测值减一点余量"就是为了不让它被读成"达标"。
    真正修好后，应把 `STABILITY_FLOOR` 一次性抬到 ≥0.8，那时它才是一条合格线。
    """
    s = measured["stability"]
    assert s >= STABILITY_FLOOR, (
        f"page_key 同页稳定性 {s:.0%} 低于地板 {STABILITY_FLOOR:.0%}（基线 12%）\n"
        f"{_report(measured)}\n"
        f"→ 退化了。注意稳定性是**分 App** 的：先看上面分 App 的数字，"
        f"判断是整体退化还是某个 App 掉了（个别 res-id 乱序 App 的低基线是已知且已记录的）")


def test_cross_round_tried_reuse_is_measured(measured: dict) -> None:
    """跨轮 `tried` 复用的命中率：**当前只有 10%，增量探索基本没生效**。

    这是 那条"增量探索疑已静默失效"的**离线复现**：
    `tried` 存的是 `(page_key, bounds)`，而 `page_key` 跨观测不稳 ⇒ 第 2 轮拿到的 key 对不上，
    历史分支几乎全被当成"没试过"，于是重复点。

    本测试同时锁住**瓶颈归属**：坐标重合度 86.5% ⇒ 坐标是稳的，问题在 key。
    谁把 key 修稳了，这里的 reuse 会跳到 ~86%；那时把 `MIN_CROSS_ROUND_REUSE` 抬到 0.7。
    """
    reuse = measured["xround_reuse"]
    assert reuse >= MIN_CROSS_ROUND_REUSE, (
        f"跨轮 tried 命中率 {reuse:.1%} 低于地板 {MIN_CROSS_ROUND_REUSE:.0%}"
        f"（{measured['xround_hit']}/{measured['xround_total']}）\n{_report(measured)}")


def test_bounds_are_stable_so_the_bottleneck_is_the_key(measured: dict) -> None:
    """坐标重合度必须保持在 70% 以上——用来**证明瓶颈在 key 而不是坐标**。

    没有这条，`page_key` 不稳时容易误判成"控件位置变了"而去改别的地方（例如给坐标加容差），
    那是治错了病。
    """
    ov = measured["bounds_overlap"]
    assert ov >= MIN_BOUNDS_OVERLAP, (
        f"坐标重合度 {ov:.1%} 低于 {MIN_BOUNDS_OVERLAP:.0%}\n{_report(measured)}\n"
        f"→ 坐标本身在漂，「跨轮复用失效」就不能再单归因于 page_key 了")


def test_identity_matching_beats_page_key_for_reuse(measured: dict) -> None:
    """**按包含度认页**必须显著优于 `page_key` 精确匹配——这是 2026-10-01 修复的门禁。

    旧机制：`tried` 以 `(page_key, bounds)` 为键，而 `page_key` 跨轮不稳 ⇒ 实测跳过率 **10%**，
    增量探索形同虚设。新机制按信号包含度认页（且要求明显胜出）⇒ 实测跳过率 **64.8%**。
    """
    assert measured["id_skip_rate"] >= MIN_IDENTITY_SKIP, (
        f"认页跳过率 {measured['id_skip_rate']:.1%} 低于地板 {MIN_IDENTITY_SKIP:.0%}"
        f"（实测 64.8%）\n{_report(measured)}\n"
        f"→ 认页退化了。检查 `IDENTITY_MATCH` / `IDENTITY_MARGIN` 或 `_identities` 的口径")
    assert measured["id_skip_rate"] > measured["xround_reuse"] * 2, (
        f"认页跳过率 {measured['id_skip_rate']:.1%} 没有明显优于 page_key 精确匹配"
        f"（{measured['xround_reuse']:.1%}）——那这次修复就没意义了")


def test_identity_matching_wrong_skip_stays_low(measured: dict) -> None:
    """**错跳率**必须压得很低：认错页会静默跳过本页的真实分支，是丢覆盖。

    实测 0.5%（`IDENTITY_MARGIN=0.05` 的功劳）。不加"明显胜出"判据时是 12.5%——
    所以这条断言同时守着"别把间距判据删掉"。
    """
    assert measured["id_wrong_rate"] <= MAX_IDENTITY_WRONG, (
        f"错跳率 {measured['id_wrong_rate']:.1%} 超过上限 {MAX_IDENTITY_WRONG:.0%}"
        f"（认对贡献 {measured['id_hit_right']}、**认错贡献 {measured['id_hit_wrong']}**）\n"
        f"{_report(measured)}\n"
        f"→ 认页变得激进，开始认错页。先确认 `IDENTITY_MARGIN` 还在生效")


def test_candidate_pool_baseline(measured: dict) -> None:
    """候选池基线：真实页面的可点候选有多少（启发式探索的选型依据）。

    实测中位 36 个，而典型探索预算是 9 步 ⇒ "点哪个"几乎决定探索质量。
    这里只做一个宽松下限，防止 `_candidate` 被改窄到把探索饿死。
    """
    assert measured["cand_median"] >= 10, (
        f"每页候选中位只有 {measured['cand_median']}——候选池被改窄了\n{_report(measured)}")


def test_report(measured: dict) -> None:
    """把口径与数字打出来，便于人工核对（`pytest -s` 可见）。"""
    print("\n[页面身份语料] " + _report(measured))
    print(f"[结构等价评估] rid-only Jaccard 同页中位 {measured['struct_within_median']:.2f}"
          f"（p25 {measured['struct_within_p25']:.2f}）vs 跨页中位 {measured['struct_cross_median']:.2f}"
          f"；J≥0.5 跨页误判 {measured['struct_cross_false']}/{measured['struct_cross_pairs']}"
          f"（{measured['struct_cross_false'] / max(1, measured['struct_cross_pairs']):.1%}）")
    assert measured["trees"] >= 200 and measured["multi_pages"] >= 20, (
        f"语料规模异常偏小：{_report(measured)}")


# -- 结构等价认页的可行性实测（2026-10-16） --------------------------------
# 背景真机案例（tx-news）：轮播「当前位」rid（tv_left_curr）稳定但内容轮换，点击后
# 落点是**轮换后的另一篇**详情页——若「同 activity + rid 结构重合」可认"同类页"，
# 走链落点核对就能放行这类轮换（任务②候选方案 pages_equivalent_layout）。

#: 结构等价的跨页误判上限。实测 **6.3%（90/1421）**——远差于 identities 版的 0.3%
#: （跨页误判门禁 MAX_CROSS_FALSE_MERGE）。
MAX_STRUCT_FALSE = 0.10

#: 结构等价的同页中位分离度地板。实测同页中位 **0.82** vs 跨页中位 **0.02**——
#: 中位间隔（0.80）与 identities 版相当，说明 rid 结构在中位意义下判别力是好的。
MIN_STRUCT_WITHIN = 0.50


def test_layout_equivalence_measured_not_adopted(measured: dict) -> None:
    """记录「结构等价（同 activity + rid Jaccard）认页」的可行性数据。

    **结论（2026-10-16 实测）：数据不支持把它接进落点判据**，只固化为观测指标：
      - 中位分离度很好（同页 0.82 vs 跨页 0.02），但**尾部双向失败**：
        ① 同页 p25 仅 0.19——内容型页面（列表翻页/轮换）rid 集差异大，会漏认；
        ② 跨页存在 J=1.00 的**模板复用**页对（如某 App 的美食频道 vs 酒店民宿，
        rid 全同）——阈值放多低都会把这类真不同页放进来（6.3% 误判 vs
        identities 版 0.3%）。
      - 若哪天要启用，先解决 ②（模板页需要额外判别信号），本测试的两条门禁
        会在判别力恶化时报警。
    """
    w = measured["struct_within_median"]
    c = measured["struct_cross_median"]
    assert w >= MIN_STRUCT_WITHIN, (
        f"同页 rid-Jaccard 中位 {w:.2f} < 地板 {MIN_STRUCT_WITHIN:.2f}（基线 0.82）\n"
        f"→ rid 结构的判别力退化了（检查 is_degenerate_res_id / parse_nodes 口径）")
    assert c <= w - 0.30, (
        f"同页/跨页 rid-Jaccard 间隔只有 {w - c:.2f}（要求 ≥0.30，基线 0.80）\n"
        f"→ 结构信号不再能区分同页与跨页")
    rate = measured["struct_cross_false"] / max(1, measured["struct_cross_pairs"])
    assert rate <= MAX_STRUCT_FALSE, (
        f"结构等价 J≥0.5 跨页误判率 {rate:.1%} > 上限 {MAX_STRUCT_FALSE:.0%}"
        f"（{measured['struct_cross_false']}/{measured['struct_cross_pairs']}，基线 6.3%）\n"
        f"→ 模板复用页变多或 rid 抓取口径变了")


# -- 状态指示文案不该参与页面签名（根因） --------------------------------
# 本用例**不依赖语料**（不用 `measured` fixture），所以在公开仓库里也会跑。

def test_state_indicator_text_does_not_split_the_page():
    """「已开启/已关闭」这类**状态指示**文案不该参与页面签名。

    否则**设备状态一变，同一个页面就被当成另一个页面**——而探索自己会拨开关。

    实测根因（2026-10-02，系统设置）：探索点了蓝牙开关 ⇒ 同页文案「已关闭」变「已开启」
    ⇒ `page_key` 变化 ⇒ 下一轮从"不同页"开始、结果只有 1/3。三轮
    `[13步/11页, 5步/4页, 13步/11页]`，L0 的 key 在 `3c6d…` 与 `05f7…` 之间**周期 2 振荡**。
    按真实 controls 复现哈希：修复前两个 key，**修复后三轮同一个 key**（`b8da…`）。
    """
    from insta_engine.page_index import page_key

    def xml(summary: str, label: str = "蓝牙") -> str:
        return ('<node class="TextView" text="' + label + '"'
                ' resource-id="android:id/title" bounds="[0,0][10,10]"'
                ' package="com.android.settings"/>'
                '<node class="TextView" text="' + summary + '"'
                ' resource-id="android:id/summary" bounds="[0,10][10,20]"'
                ' package="com.android.settings"/>')

    assert page_key(xml("已关闭"), "com.android.settings") == \
        page_key(xml("已开启"), "com.android.settings"), \
        "开关状态变化不该改变页面身份（否则探索拨一下开关，下一轮就从'另一页'开始）"

    # 反例：**标签**变了页面就该变——状态规则不能把整页抹平
    assert page_key(xml("已关闭", "蓝牙"), "com.android.settings") != \
        page_key(xml("已关闭", "WLAN"), "com.android.settings"), \
        "标签变化必须改变页面身份（规则只能抹掉状态词，不能抹掉标签）"

    # 反例：不能误伤正常文案
    from insta_engine.page_index import is_dynamic_text
    for keep in ("蓝牙", "WLAN", "移动网络", "超级终端", "更新日志", "开", "关"):
        assert not is_dynamic_text(keep), f"{keep!r} 是正常文案/标签，不该被当动态文本"


def test_digest_page_must_be_given_package_or_system_ui_leaks():
    """`digest_page` **必须传被测包名**，否则系统 UI 会漏进摘要。

    这不是 `digest_page` 的错——它按设计就用 `package` 过滤（docstring 写着「过滤 IME 等外部窗口」）。
    错的是**调用方忘了传**：`runner.py` 的失败上下文里调 `digest_page(self.drv.dump())`（`package=""`），
    过滤失效；而**系统 UI 节点在 a11y 树里更靠前** ⇒ 取前 8 条文案时**全被状态栏/导航栏占满**，
    App 自己的控件一个都进不来。

    实测后果（2026-10-03，T28）：LLM 归因看到「可见控件 = 返回/主屏幕/切换输入法」
    就判「页面是系统界面、前置没生效」，把**真产品缺陷**误判成 `precondition` ⇒ 漏报。
    """
    from insta_engine.page_index import digest_page

    xml = ('<node class="TextView" text="11:51" package="com.android.systemui"'
           ' bounds="[0,0][10,10]"/>'
           '<node class="Button" text="App按钮" package="com.app"'
           ' clickable="true" bounds="[0,10][10,20]"/>')

    with_pkg = [c.get("text") for c in digest_page(xml, "com.app")["controls"]]
    assert with_pkg == ["App按钮"], f"传包名应只留被测 App 的控件，实际 {with_pkg}"

    without = digest_page(xml, "")["controls"]
    assert len(without) == 2, (
        f"不传包名时系统节点会一起进来（这正是踩过的坑），实际 {len(without)} 条")
