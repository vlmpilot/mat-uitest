"""启动落点是不是首页 —— 三档 `home` / `not-home` / `unknown`（静默）。

## 为什么需要它（2026-10-29 现场 → 2026-10-31 标定）

2026-10-29 靶场 r3 里 `gen-183555-1..4` **整组跑在 tx 搜索页**上：一次带走 4 条，
把极差从 1.5pp 拉到 7.6pp。而三道现成判据**全都不认**：

- `false_red`（`runner.py`）：判据是"判了 env-failure 但 App 在前台" ⇒ App 就是在前台，不认；
- `off_page`（`off_page.py`，R16）：只认 `left-app` / `webview-page` ⇒ 搜索页仍是 tx，不认；
- `classify_run_failures.py`：反而判成「用例问题（A2 断言与导航耦合）」——**方向对、结论过重**。
  它抓到的真问题是"启动必须落在首页"这个**隐式前置没人置备**（`arrival.reason` 原文：
  `推断不出唯一目标页`），但"必失败"不成立（同批 r1/r2 全过）。按它的结论去改用例，
  恰恰会把这 4 条写得最对的断言改松（断频道栏 `selected` 是真断言，改松立刻变假绿）。

## 判据

    score = Jaccard(chrome_signals(当前屏), chrome_signals(图谱根页))

参考集取**图谱里 `depth` 最小的那一页**（正常就是唯一的 `depth=0`）：那是探索的起点，
也就是**启动落点**本身。实测 tx = `SplashActivity` 里的信息流首页（频道栏 + 底栏 + 搜索框），
xhs = `IndexActivityV2` 的「发现」首页（顶部三页签 + 底栏）。

⚠️ **不要用 `home_labels_for(max_depth=1)`**（现成的，但默认值会把人带偏）：实测 tx 图谱
**59 页里 58 页都在 depth 1**（详情/登录/我的/WebView/Hippy 全在里面）⇒ 参考集变成
"这个 App 显示过的一切"，详情页也能拿高分。xhs 同理（depth 1 含我页/消息页/11 个市集类目页）。

## 阈值是标定出来的，不是拍脑袋（`scripts/calibrate_home_sentinel.py` 可复跑）

481 条可判屏幕（`artifacts/**/screens/*.json` 全量）。标签按**内容信号**给，不是按分数
（按分数打标签是循环论证）：每个包各取一个**只有首页才有**的内容信号（频道栏 rid / 页签文案 / 首页专属文案），具体取值见标定脚本。

| 包 | 首页簇 | 非首页簇 |
|---|---|---|
| tx | 0.371 ~ 0.795（中位 0.769） | 0.000 ~ 0.184（中位 0.015） |
| xhs | 0.500 ~ 0.577 | 0.026 ~ 0.171 |
| mt | 0.409 ~ 0.476 | 0.031 ~ 0.048 |

把参考集换成**首页的每一个样本**取最差情形（图谱会被重建，重建后的根页是另一次观测）：
tx min_home 0.353 / max_non 0.233、xhs 0.667 / 0.231、mt 0.771 / 0.031
⇒ 三包共同的安全区间 **0.233 ~ 0.353**，取中点 **0.293**。

事发那 4 条的直接对照（同用例同一步，只差落点）：r1 首页 **0.698~0.795**、r3 搜索页 **0.021**。

## 四条纪律

- **只打标，不改判定**：`status` / `category` 一律不动（与 R16 同一条纪律——改类别会让
  历史基线不可比）。
- **三档，不是两档**：`unknown` 必须**静默**。没图谱 / 图谱没 `depth` / 根页没骨架信号 /
  这一屏没有被测包控件 / 信号条数 < `MIN_SIGNALS` ⇒ **判不了**，不是"不是首页"。
  实测存量 8 条"0 控件"现场全是**别的 App 在前台**（`foreground=com.example.im` 等），
  把它们记成"落点错"就是凭空造一个误报源。
- **判一次不够**：首屏可能是"还在渲染"。实测 `tx-f02-tab-mine-and-back/step_07` 回首页那
  一刻 `after` 里只剩底栏 16 个控件、信息流和频道栏全没 ⇒ 单次采样会把"加载中"读成
  "落错页"。所以判出 `not-home` 时**再看一眼**，两次都 `not-home` 才认：渲染中的页面会
  往上走，真的落错页不会。
- **判据只有一处实现**：`runner._note_landing`（跑的时候写 `run.json`）与
  `scripts/classify_run_failures.py`（回头审产物）都调这里，不各写一份。

⚠️ **历史产物只能部分回溯**：`screens/` 证据**只在失败时落盘**，而 `launch` 通常成功
⇒ 老产物没有 launch 那一屏。判读工具的回退路径只在"launch 之后到第一个失败步之间
全是 skipped/failed"时成立（那时第一个失败步的 `before` 就是落点那一屏），且必须
**排除"置备动过手"**：`_ensure_arrival` 在 launch **之后**的步前才跑，`arrival.hops>0`
时回读到的 `before` 是**被置备带过去**的那一页，不是落点（实测 2026-10-25 `xhs-5`/`xhs-6`
5 条就是这样，真因是 R18 的 tiebreaker）。
这与 R16 不同——R16 能全量回溯，是因为它判的本来就是**失败步**。
"""

from __future__ import annotations

from .page_index import chrome_signals

#: 落点就是首页。
HOME = "home"
#: 落点不是首页（疑似上一轮残留的页面 / 启动没走完）。
NOT_HOME = "not-home"

#: 判"是首页"的骨架相似度门槛。标定过程见模块 docstring：
#: 三包共同安全区间 0.233~0.353，取中点。
HOME_SIM = 0.293

#: 判"这一屏够不够判"的信号条数下限。存量 481 条可判屏里**最少的真实页面是 4 条**
#: （tx 搜索结果页的纯文案骨架，零 rid）；再少只能是"还在加载"或"整屏不是被测包"。
#: 低于它的屏返回 `unknown`（静默），不当"不是首页"。
MIN_SIGNALS = 4


def home_profile(index: dict) -> frozenset[str]:
    """图谱里**启动落点页**（`depth` 最小）的骨架信号。

    取不到就返回空集，调用方按"判不了"静默 —— 与 `home_labels_for` 的"无索引不瞎猜"同口径。
    """
    pages = [
        p for p in (index or {}).get("pages") or []
        if isinstance(p, dict) and isinstance(p.get("depth"), int)
    ]
    if not pages:
        return frozenset()
    root = min(pages, key=lambda p: p["depth"])
    return chrome_signals(root.get("controls") or [])


def landing_kind(controls: list[dict] | None, profile: frozenset[str],
                 threshold: float = HOME_SIM) -> tuple[str | None, float | None]:
    """返回 `(kind, score)`；`kind is None` = **判不了**（调用方必须静默）。

    `controls` 是 `digest_page(xml, package)["controls"]`（已按被测包过滤），
    所以"空列表"等价于"这一屏没有被测 App 的控件"，属于判不了。
    """
    if not profile:
        return None, None
    signals = chrome_signals(controls or [])
    if len(signals) < MIN_SIGNALS:
        return None, None
    score = len(signals & profile) / len(signals | profile)
    return (HOME if score >= threshold else NOT_HOME), round(score, 3)


def describe(kind: str | None) -> str:
    """给人看的短说明（runner 的 stderr 告警与判读工具共用一套措辞）。"""
    return {
        HOME: "落在首页",
        NOT_HOME: "落点不是首页：疑似上一轮残留的页面，或启动没走完",
    }.get(kind or "", "")
