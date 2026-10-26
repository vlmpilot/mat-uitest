"""探测期安全词表（单一事实来源）。

页面爬取（mat index）与自主探索（mat explore）共用同一份防误点黑名单——
此前两处口径漂移（9 词 vs 15 词），导致 `mat index` 会点「退出登录/发布/评价」，
直接把被测 App 登出或产生真实副作用（外部评审 P1-1，安全相关）。
接线由 test_safety_wiring 守卫：两处引用必须是**同一对象**。
"""
from __future__ import annotations

import re

# 命中任一关键词的可点控件，爬取与探索均不点击（探测不是交易）
SENSITIVE_WORDS = (
    "支付", "付款", "结算", "确认订单", "提交订单", "客服", "电话", "充值", "转账",
    "拨号", "订单", "晒单", "评价", "发布", "分享", "退出登录",
    # 权限触发类（2026-10-12 补）：点了会弹**系统**权限弹窗（RECORD_AUDIO/CAMERA/
    # LOCATION/CONTACTS/媒体…），弹窗不属于被测包 ⇒ 爬取收不到控件还会把 App
    # 挡在前台（实测：津云搜索页麦克风图标 → 录音权限弹窗，任务结束后残留挡屏）
    "麦克风", "录音", "语音", "相机", "拍照", "拍摄", "扫一扫", "扫描",
    "定位", "位置", "通讯录", "联系人", "相册",
)

# 回退/关闭语义（2026-10-16）：点了不产生「前进导航边」——把它记成 parent_action
# 的链，重放时导航起点根本没有该控件 ⇒ 从根上不可重放（实测 tx-news：explore 在
# 首页点「返回」到达的页，arrival 重放 0/3 全死在第 1 跳；且 graph_repair 借链会
# 把坏链扩散给相似碎片，所以 repair 也按同一词表排除）。
# ⚠️ 文案判据刻意**全等**（「返回首页」这类复合词不误伤）；res-id 是工程师命名，
# 按词边界匹配，信号比文案强。
BACK_WORDS = frozenset({"返回", "返回键", "后退", "关闭", "取消", "收起",
                        "Back", "Close", "Cancel"})
BACK_RID_RE = re.compile(r"[/_.-](?:back|close|cancel|dismiss)(?:[/_.-]|$)", re.I)


#: 无障碍标签里跟在按钮名后的后缀（「返回 按钮」「返回按钮」「返回图标」）
_A11Y_SUFFIXES = ("按钮", "图标", "按键", "button", "icon")


#: **广告位**的 rid 标记（2026-10-23 实测 tx-news）。信息流广告卡片点进去会把设备
#: 跳到**应用市场**（实测一轮离场 18 次，每次要 2 帧拉回，占掉约 20% 的帧预算）。
#: 实测证据：首页 21 条 `tried` 里两条的 rid 是 `com.tencent.news:id/txt_streamAd_title`
#: —— 广告位有**稳定的 rid 命名**，不必靠文案猜。
#: ⚠️ 判据**只看 rid、且要窄**：按文案（"秒杀"/"好价"）判会误杀真新闻标题；
#: 宁可漏掉几个广告，也不要误伤功能入口（漏掉的还有"离场后学习"兜底，见 explore）。
_AD_RID_MARKERS = (
    "streamad", "ad_title", "ad_title_", "adcard", "ad_card", "adview", "ad_view",
    "advert", "promotion", "banner_ad", "splash_ad", "adslot", "ad_slot",
)


def is_ad_slot_rid(rid: str | None) -> bool:
    """这个 res-id 是不是**广告位**（点了多半会离场到应用市场/落地页）。"""
    value = str(rid or "").lower()
    return bool(value) and any(m in value for m in _AD_RID_MARKERS)


def is_back_word(label: str) -> bool:
    """文案是否回退/关闭语义。

    先按**全等**判（避免误伤「返回首页」这类复合词），再额外认一种形态：
    **回退词 + 无障碍后缀**（`返回 按钮` / `返回按钮` / `返回图标`）。

    ⚠️ 为什么必须认这种形态（2026-10-21 实测）：accessibility label 常写成
    「返回 按钮」，全等判据认不出 ⇒ 被当成**前进导航**：graph-repair 把这种链
    借给别的页（某生活服务 App一次借出 24 条），真机重放时第一跳就是点「返回」——起点页
    根本没有这个控件，必然失败。同一坑在 `page_index.hop_replayable` 里也走这个函数。
    """
    text = str(label or "").strip()
    if text in BACK_WORDS:
        return True
    squeezed = re.sub(r"\s+", "", text).lower()
    for suffix in _A11Y_SUFFIXES:
        if squeezed.endswith(suffix) and squeezed[: -len(suffix)] in {
                w.lower() for w in BACK_WORDS}:
            return True
    return False


def is_back_rid(rid: str) -> bool:
    """res-id 是否 back/close/cancel/dismiss 语义（词边界）。"""
    return bool(BACK_RID_RE.search(str(rid or "")))


# 瞬态启动帧语义（2026-10-16）：splash/开屏广告页上的「跳过」类控件是**一次性**的——
# launch 后该页被 App 自动翻过（计时到点或广告播完），以它为链根的父链结构性不可
# 重放：第 1 跳是对「帧还活着」的竞态，真机重放 0/15 全死于「第 1 跳未找到可点控件
# （文案「跳过」）」（实测 tx-news：5 条 splash 根链）。判据与 BACK_WORDS 同风格：
# **全等**，避免误伤含「跳过」字样的内容文本（如「跳过训练营」）。
SPLASH_SKIP_WORDS = frozenset({"跳过", "跳过广告", "关闭广告", "skip", "Skip"})


def is_splash_skip_label(label: str) -> bool:
    """文案是否「跳过」类瞬态控件语义（全等，见上方说明）。"""
    return str(label or "").strip() in SPLASH_SKIP_WORDS
