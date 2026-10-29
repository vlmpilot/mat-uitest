"""本步失败时，「这一屏还是不是用例假设的那个页面」——**只读已有证据，零设备往返**。

## 为什么需要它（R16，2026-10-29）

`locate-fail` 里混着两种完全相反的东西：

1. **定位值写错**（用例问题）——目标就在这一屏上，`text=我的` 写错了；
2. **这一屏根本不是用例假设的那一屏**——点「我的」之后插屏广告把 App 内换成了
   全屏 WebView 落地页，或者干脆把**微信小程序**拉起来了。

第 2 种在 `run.json` 里和「目标不在可见控件」长得一模一样，于是 `classify_run_failures.py`
的 B2 判据会把它读成「用例/前置条件问题」——直接污染首跑通过率的「偏引擎 vs 偏用例」判读
（"49 次失败判读、偏引擎 0 条"这类统计如果含这种样本，结论方向是反的）。

## 判据是标定出来的，不是想出来的

在 355 份存量失败现场（`artifacts/**/screens/*.json`）上量过：

| 签名 | 命中 | 误报 |
|---|---|---|
| `left-app`（前台不是被测包） | 9 | 0（6×tx→小程序、1×tx→外卖应用、3×→demo App，`after_packages` 都真含该包） |
| `webview-page`（整屏只剩 1 个 WebView 页） | 2 | 0 |

阈值 `_WEBVIEW_PAGE_MAX_CONTROLS = 30` 的依据：存量里「恰好 1 个 WebView」的现场只有 12 个，
**天然分成两簇**——≤22（装修/腕表/登录/淘宝精选，全是广告落地页）与 ≥35（tx 首页、
文章详情「从阅读者到共创者…」）。30 落在两簇中间的空隙里。

## 三条纪律

- **只打标，不改判定**：`status` / `category` 一律不动。它和 `false_red` 同族——单独计数，
  而不是把一条 `locate-fail` 悄悄改成别的类别（那会让历史基线不可比）。
- **宁缺毋滥**：`foreground` 取不到（空串）时不判；控件列表为空时不判（多半是还在加载，
  而"前台就是被测 App 却没有它的控件"这件事本身信息量太低）。
- **判据只有一处实现**：runner（跑的时候写进 `run.json`）与
  `scripts/classify_run_failures.py`（回头审旧产物）都调这里，不各写一份。
"""

from __future__ import annotations

#: 前台是被测 App，但整屏只剩一个 WebView 页（标题栏 + WebView），且被测包自己的
#: 控件少于阈值 ⇒ 被 App 内全屏广告/外链换页了。
WEBVIEW_PAGE = "webview-page"
#: 前台**不是**被测包 ⇒ 被别的 App（微信小程序、电商 App）拉走了。
LEFT_APP = "left-app"

#: 「整屏只有一个 WebView 页」的控件数上限。标定过程见模块 docstring：
#: 存量两簇是 ≤22 / ≥35，取中间值 30。
_WEBVIEW_PAGE_MAX_CONTROLS = 30


def off_page_kind(package: str, after_controls: list[dict] | None,
                  foreground: str) -> str | None:
    """返回 `"left-app"` / `"webview-page"` / `None`（=这一屏看起来还是被测 App 自己的页面）。

    参数全部来自 `screens/step_*.json` 已有字段，所以**能对历史产物回溯判定**，
    不需要重跑（存量 355 份现场就是这么做标定的）。
    """
    if not package:
        return None
    if foreground and foreground != package:
        return LEFT_APP
    controls = after_controls or []
    if not controls:
        return None
    webviews = [c for c in controls if "WebView" in str(c.get("class") or "")]
    if len(webviews) != 1:
        return None
    if len(controls) >= _WEBVIEW_PAGE_MAX_CONTROLS:
        return None
    # WebView 自己得有标题文案：没有文案的空白 WebView 更像"还在加载"，
    # 而"加载中"不该被说成"被换页"（否则失败原因会被推给一个不存在的广告）。
    if not str(webviews[0].get("text") or "").strip():
        return None
    return WEBVIEW_PAGE


def describe(kind: str | None) -> str:
    """给人看的短说明（报告/判读工具共用一套措辞）。

    ⚠️ 这里**不要用括号**：判读工具会把它套进 `现场已换页（…）`，嵌套括号读起来像打错。
    """
    return {
        LEFT_APP: "被别的 App 拉走：前台已不是被测应用",
        WEBVIEW_PAGE: "App 内被换成全屏 WebView 页：广告/外链落地页",
    }.get(kind or "", "")
