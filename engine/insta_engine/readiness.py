"""图谱准备度（readiness）——把「还该补哪一步」变成可见的建议。**只提示，不设闸门。**

## 为什么不做硬闸门（2026-10-17 评估结论）

曾考虑过「执行用例前强制先遍历探索 + 覆盖率 >80% 才准出」，评估后否掉，四条依据：

1. **收益未被证实**：实测给生成注入图谱上下文**无显著效应**（A/B 实测：+3.5pp/−6.4pp，p>0.3）
   ⇒「先建图谱 ⇒ 生成更准」这条因果链目前测不出来，为它设硬门槛是在赌未证实的收益；
2. **图谱质量撑不起门槛**：认页同页稳定性仅 **12%**（A/B 实测）——拿它当准出条件，
   是把用户挡在质量不稳的门后；
3. **失败主因不在图谱**：92% 的失败是用例写法/前置状态问题，**没有一条是定位能力不足**
   （用例质量评测的同批结论）⇒ 图谱完美也救不了写法差的用例；
4. **代价可指认**：首次体验从 3 分钟变数小时；`explore` 撞风控默认即停（`MAT_RISK_POLICY=stop`），
   有风控的 App 会让用户卡在门口；门槛还会诱发「灌水用例刷覆盖」——假信号比没指标更糟。

所以本模块**只算状态、只给建议**：调用方（CLI / 配置台）自行决定怎么展示，
用户的决策权不被剥夺。

## 判据来源

「已复核」= 图谱页 `title_source == "manual"`（人工复核写入，此后 `describe`/merge 都不覆盖）。
这是图谱里**唯一**能可靠区分「机器猜的」与「人确认过的」字段。
"""

from __future__ import annotations

from pathlib import Path


def index_path(root: Path | str, package: str) -> Path:
    """图谱路径（与 runner / page_index 同约定：<root>/artifacts/page_index/<包名>.json）。"""
    return Path(root) / "artifacts" / "page_index" / f"{package}.json"


def readiness(root: Path | str, package: str) -> dict:
    """某包的图谱准备度。**只读**：不写图谱、不调模型、不需要设备。"""
    from .page_index import load_index

    data = load_index(index_path(root, package)) or {}
    pages = [p for p in (data.get("pages") or []) if isinstance(p, dict)]
    reviewed = sum(1 for p in pages if p.get("title_source") == "manual")
    return {
        "package": package,
        "has_index": bool(pages),
        "pages": len(pages),
        "reviewed": reviewed,
        "unreviewed": len(pages) - reviewed,
    }


def summary(r: dict) -> str:
    """一行状态摘要（给界面显示）。"""
    if not r.get("has_index"):
        return f"{r.get('package', '')}：还没有页面图谱"
    return (f"图谱 {r['pages']} 页 · 已复核 {r['reviewed']} 页"
            f"（未复核 {r['unreviewed']}）")


def hint(r: dict, ui: str = "cli") -> str | None:
    """给一条**最值得做**的下一步建议；无可建议时返回 None（别刷无意义的提示）。

    优先级：没有图谱 > 有图谱但有未复核页。理由：前者决定生成能力的下限，
    后者是「提升最直接的一步」（人工确认的页名会成为生成时的页面语义）。
    ``ui``：`"cli"`（默认，建议里给 mat 命令）或 `"console"`（建议里给配置台页面/按钮
    ——控制台用户在界面上，让他去终端敲命令是断头路）。
    """
    pkg = r.get("package") or "该 App"
    if not r.get("has_index"):
        return (f"还没有 {pkg} 的页面图谱：生成只能凭控件文案猜，命中率会明显下降。"
                + ("点「构建页面图谱」直接开始（约 1-2 分钟，会自动点选可安全点击的"
                   "控件探索页面，不装卸应用、敏感操作有黑名单），"
                   "或到「遍历探索」页真机遍历（同时发现崩溃/白屏）。" if ui == "console" else
                   f"建议先跑 `mat index {pkg}`（约 1-2 分钟，会自动点选可安全点击的控件"
                   f"探索页面，不装卸应用、敏感操作有黑名单）"
                   f"或 `mat explore {pkg}`（真机遍历，同时发现崩溃/白屏）。"))
    if r.get("unreviewed"):
        return (f"{pkg} 的图谱有 {r['unreviewed']} 页未复核（共 {r['pages']} 页）："
                f"复核页名后，生成的用例引用的是你确认过的页面语义——这是提升生成准确度"
                f"最直接的一步（"
                + ("到「页面复核」页，筛选后可「批量复核」）。" if ui == "console" else
                   f"配置台「页面复核」，或 `mat review {pkg}`）。"))
    return None


#: 遍历探索建议（2026-10-17 定；2026-10-26 用户反馈去掉「甜点判据」术语，判据内容保留）——
#: 给**判断规则**，不给魔法数字：不写「跑 N 分钟最好」（实测没有时长-收益曲线，
#: A/B 实测图谱上下文注入无显著效应），可复用的判据只有**边际新增页面是否还在增长**。
EXPLORE_TIP = (
    "探索到什么程度算够？看「新增页面数」是否还在增长——还在涨就继续，"
    "连续不再新增就收手（不是固定时长）。"
    "起步建议 5-10 分钟 / 100-200 步 / 深度 ≤L3 覆盖主干；"
    "深层页面常需特定业务状态才出现，硬加深多半是空转，还可能撞到风控被中断（默认即停）。"
    "探索完收益最大的一步是复核页名，然后才去生成用例。"
)
