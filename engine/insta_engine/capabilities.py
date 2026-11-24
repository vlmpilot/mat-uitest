"""平台能力集合（单一事实源，2026-11-24 评审 #9）。

## 为什么要有这个文件

"某平台支不支持某能力"此前分散在 5 处判断（`cmd_index` / `cmd_explore` / `cmd_arrival` /
`web_api/indexing.py` / `web_api/runs.py`），加 MCP 的文档字符串，一共 6 个说法——已经
出现过不一致：`mat_explore` 的 MCP 文档写「仅 Android」，实际会把鸿蒙请求传下去执行。

现在所有拦截与提示都读这里；文档（README 平台表）也按这里生成，并用测试守护一致性
（参照 `test_policy_parity.py` 的做法：两边不一致就红）。

## 三态而不是布尔

`True` / `"beta"` / `False` 必须分开，否则只能得到两种谎话：

- `True`：平台支持，且**已验证**；
- `"beta"`：能跑，但**已知有缺口**（例如鸿蒙的崩溃检测拿不到系统日志 ⇒ 崩溃可能被
  归成别的分类）。调用方应当放行但提示，不能假装完整；
- `False`：平台本身不支持（如 iOS 真机录屏由系统限制），或尚未实现且**短期内不做**。

把 `"beta"` 写成 `True` 会让用户以为结果可信；写成 `False` 会让可用能力被藏起来。
"""

from __future__ import annotations

#: 能力键 → 人读说明（README 平台表与报错文案都从这里取）
KEYS: dict[str, str] = {
    "run": "执行用例（run / verify）",
    "index": "页面图谱（index）",
    "explore": "探索测试（explore）",
    "arrival": "自动到达目标页（arrival 前置置备）",
    "xpath": "xpath 定位",
    "evidence": "系统日志证据（崩溃归类为 app-crash）",
    "app_info": "读取被测 App 版本号",
    "record": "录屏",
    "recents": "最近任务",
    "install": "安装 / 重装 APK",
}

#: 平台 → 能力 → True / "beta" / False
CAPABILITIES: dict[str, dict[str, bool | str]] = {
    "android": {
        "run": True,
        "index": True,
        "explore": True,
        "arrival": True,
        "xpath": True,
        "evidence": True,
        "app_info": True,
        "record": True,
        "recents": True,
        "install": True,
    },
    "ios": {
        "run": True,
        # 页面图谱/探索/arrival 依赖"按包名拉起 + 前台过滤"，iOS 驱动没有 shell 通道，
        # 真机还要处理证书与解锁（评审 #8）。未实现，不是平台限制。
        "index": False,
        "explore": False,
        "arrival": False,
        "xpath": True,
        "evidence": True,      # WDA syslog
        "app_info": True,
        # iOS 真机录屏受系统限制，已明确不做（评审 #8 的说明）。
        "record": False,
        "recents": True,
        "install": False,      # 真机装包走 Xcode/证书流程，不由本工具代劳
    },
    "harmony": {
        "run": True,
        # 拉起走驱动 `app_start`（aa start），已不再依赖 shell 的 monkey；
        # 但崩溃检测还没有 hilog 采集 ⇒ explore 标 beta（结果可用、崩溃可能漏判）。
        "index": True,
        "explore": "beta",
        "arrival": False,      # 依赖页面图谱的父链置备，尚未适配
        "xpath": False,        # 首版不支持，见 HarmonyDriver.find_xpath 的报错
        "evidence": False,     # 未采 hilog ⇒ 崩溃无法归 app-crash
        "app_info": True,      # bm dump -n 解析版本号（2026-11-24 补）
        "record": False,
        "recents": False,
        "install": False,
    },
}

#: 能力不足时的**正确提示**（拦住时给原因，不是干巴巴一句"不支持"）
REASONS: dict[tuple[str, str], str] = {
    ("ios", "index"): "iOS 驱动没有 shell 通道，无法按包名拉起并过滤前台控件树"
                      "（页面图谱的 WDA 适配未实现）；当前 iOS 可用 mat run --platform ios",
    ("ios", "explore"): "探索靠「按包名拉起 + 前台控件树过滤」认页，而 iOS 驱动没有 shell "
                        "通道（WDA 适配未实现）；当前 iOS 可用 mat run --platform ios",
    ("ios", "arrival"): "arrival 依赖页面图谱的父链置备，iOS 无图谱",
    ("ios", "record"): "iOS 真机录屏受系统限制，已明确不做（模拟器同样未接）",
    ("ios", "install"): "iOS 装包走 Xcode/证书流程，本工具不代劳（免费证书 7 天过期）",
    ("harmony", "arrival"): "arrival 依赖页面图谱的父链置备，鸿蒙尚未适配",
    ("harmony", "xpath"): "鸿蒙首版不支持 xpath，请用 text / res-id 定位",
    ("harmony", "record"): "鸿蒙暂未接录屏",
    ("harmony", "recents"): "鸿蒙暂未接最近任务",
    ("harmony", "install"): "鸿蒙暂不支持由本工具安装 HAP",
}


def state(platform: str, key: str) -> bool | str:
    """返回该平台该能力的状态；未知平台按 Android 处理（历史行为），未知键返回 False。"""
    table = CAPABILITIES.get((platform or "android").lower(), CAPABILITIES["android"])
    return table.get(key, False)


def supported(platform: str, key: str) -> bool:
    """是否**允许尝试**（`beta` 也允许——放行但由调用方提示）。"""
    return state(platform, key) is not False


def is_beta(platform: str, key: str) -> bool:
    return state(platform, key) == "beta"


def reason(platform: str, key: str) -> str:
    """不可用时的原因；没有专门文案时给通用的一句（含能力说明，便于定位）。"""
    specific = REASONS.get(((platform or "android").lower(), key))
    if specific:
        return specific
    return f"{platform} 平台不支持「{KEYS.get(key, key)}」"


def require(platform: str, key: str) -> None:
    """不支持则抛 ValueError（文案来自 `reason`）。调用方在 CLI/API 入口用。"""
    if not supported(platform, key):
        raise ValueError(reason(platform, key))


def beta_note(platform: str, key: str) -> str | None:
    """`beta` 能力的一句提示（放行时附在人读输出里）；非 beta 返回 None。"""
    if not is_beta(platform, key):
        return None
    return (f"⚠️ {platform} 的「{KEYS.get(key, key)}」是 beta：能跑，但结果可能不完整"
            f"（{_beta_gap(platform, key)}）")


def _beta_gap(platform: str, key: str) -> str:
    if (platform, key) == ("harmony", "explore"):
        return "尚未采集 hilog，崩溃可能不会被归类为 app-crash"
    return "已知能力不完整"


def platform_table() -> list[dict]:
    """README / 手册用的能力表（一平台一行，按 KEYS 顺序）。"""
    rows = []
    for platform, table in CAPABILITIES.items():
        rows.append({"platform": platform,
                     **{key: table.get(key, False) for key in KEYS}})
    return rows


def render_markdown() -> str:
    """把能力表渲染成 Markdown（文档由能力集合生成，避免两边漂移）。"""
    head = "| 能力 | " + " | ".join(CAPABILITIES) + " |"
    sep = "|---|" + "---|" * len(CAPABILITIES)
    lines = [head, sep]
    mark = {True: "✅", False: "❌", "beta": "⚠️ beta"}
    for key, label in KEYS.items():
        cells = []
        for platform in CAPABILITIES:
            cells.append(mark[CAPABILITIES[platform].get(key, False)])
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
