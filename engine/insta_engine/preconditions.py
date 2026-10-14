"""前置条件（IR 的 `preconditions`）在 DSL 与运行期之间的处理。

## 为什么需要这个模块

导入器**正确读出了**前置条件（`manual_import.ManualCase.preconditions`；实测 xlsx 批次能提取出
「已进入资讯详情页」「位置权限尚未授予」等），但 `dsl.TestCase` 此前**没有这个字段**
⇒ 在 DSL 边界上被**静默丢弃**。后果是实测出来的：某资讯类客户端的 16 条手工用例转出来的自动化用例
**全是 `launch + 1~2 个动作`**，却去点"别的页面才有的控件" ⇒ 跑出 7 条**假的"定位失败"**
（人去查定位链，而问题根本不在那儿）。

## 三道处置（按前置条件的**三类**分开）

| 类别 | 例子 | 处置 |
|---|---|---|
| ① **环境前提** | `App 已安装`、`已完成首次隐私授权` | **不拦**（App 没装根本跑不起来，拦它会误伤全部用例） |
| ② **不可置备的系统态** | `位置权限尚未授予` | **拒跑**（动作集改不了系统权限 ⇒ 跑下去只会产出假的定位失败） |
| ③ **可置备的页面状态** | `已进入搜索结果页`、`存在至少 1 条历史搜索` | **展开成导航步骤**（或人工置备）；未展开时**报 issue** |
| ④ **随机事件** | `本次冷启动命中开屏广告` | **报 issue，不展开**（是否发生不由 MAT 也不由人决定 ⇒ 展开等于让模型去"导航到一个开屏广告"） |

⚠️ **③ 是最大的失败来源**：实测 xlsx 批次 8 条用例声明了「已进入搜索结果页」等前置条件，
跑测**全部照红**（locate-fail）—— 因为「声明了」不等于「有人会去置备它」。
MAT 的动作集（launch/tap/input/clear/swipe/scroll/back/home/key-event/wait）
**没有一个能改页面状态**，所以这类前置条件只有两条出路：**展开成导航步骤**，或**人在执行前手工置备**。

⚠️ **只拦"否定式"的权限/授权态**。「App 已安装」这种**肯定式**前置是运行的前提，
拦它会误伤**全部**用例 —— 这条边界是刻意的，别放宽。

⚠️ **分类是启发式，且默认归 ③（fail-loud）**：把环境前提误判成 ③ 只是多一条提示（人一看就知道），
而把页面状态误判成 ① 会让闸门**沉默**、用例继续照红。宁可吵，不可哑。
"""

from __future__ import annotations

__all__ = [
    "ENV", "PAGE", "RANDOM", "SYSTEM", "classify", "classify_all", "is_random_event",
    "is_unprovisionable", "provisionable", "refusal_message", "unprovisionable",
]

#: 权限 / 授权类名词：出现其一才算"权限态"。
_PERMISSION_NOUNS = (
    "权限", "授权", "隐私", "通知", "定位", "位置", "相机", "麦克风",
    "存储", "通讯录", "相册", "蓝牙", "悬浮窗", "后台",
)

#: 否定词：**只有否定式**才拦（肯定式前置是运行前提，见模块说明）。
_NEGATIONS = (
    "尚未", "未授予", "未授权", "未开启", "未允许", "未同意", "未打开", "未获取",
    "没有", "已关闭", "已拒绝", "已禁用",
)

#: 四类前置条件（见模块说明的表格）。
ENV = "env"        # ① 环境前提：不拦
SYSTEM = "system"  # ② 不可置备的系统态：拒跑
PAGE = "page"      # ③ 可置备的页面状态：展开或报 issue
RANDOM = "random"  # ④ 随机事件：报 issue，不展开

#: ① 环境前提的标记（实测出现过的：`App 已安装`、`已完成首次隐私授权`）。
_ENV_MARKERS = (
    "已安装", "未安装", "已连接", "未连接", "网络", "设备", "已完成首次",
    "已登录", "未登录", "已注册", "未注册", "已升级",
)

#: ③ 页面状态的标记（实测出现过的：`已进入搜索结果页`、`存在至少 1 条历史搜索`）。
_PAGE_MARKERS = (
    "已进入", "已打开", "已处于", "停留在", "已切换到", "已跳转", "存在至少", "当前在",
)

#: ④ **随机事件**：是否发生不由 MAT 也不由人决定（实测：`本次冷启动命中开屏广告`）。
#: ⚠️ 判据要**窄**：宽了会把本该展开的页面状态误判成随机态 ⇒ 闸门沉默、用例继续照红。
#: 所以要求「随机词」与「发生词」**同现**才算。
_RANDOM_MARKERS = ("随机", "偶发", "恰好", "本次")
_RANDOM_COOCCUR = ("命中", "出现", "发生", "弹出", "展示")


def is_unprovisionable(text: str) -> bool:
    """这条前置条件是不是 MAT **按构造无法置备**的（否定式权限/授权态）。"""
    t = str(text or "")
    return any(n in t for n in _PERMISSION_NOUNS) and any(g in t for g in _NEGATIONS)


def is_random_event(text: str) -> bool:
    """这条前置条件描述的是**随机事件**吗（既不能置备，也不能展开成导航步骤）。"""
    t = str(text or "")
    return any(m in t for m in _RANDOM_MARKERS) and any(c in t for c in _RANDOM_COOCCUR)


def classify(text: str) -> str:
    """把一条前置条件分到 ①`ENV` / ②`SYSTEM` / ③`PAGE` / ④`RANDOM`（**默认 ③**，见模块说明）。"""
    t = str(text or "")
    if is_unprovisionable(t):
        return SYSTEM
    if is_random_event(t):
        return RANDOM
    if any(m in t for m in _PAGE_MARKERS):
        return PAGE
    if any(m in t for m in _ENV_MARKERS):
        return ENV
    return PAGE


def classify_all(preconditions: list[str] | None) -> dict[str, list[str]]:
    """按四类分组（保序、去重）。"""
    out: dict[str, list[str]] = {ENV: [], SYSTEM: [], PAGE: [], RANDOM: []}
    for item in preconditions or []:
        text = str(item or "").strip()
        if not text:
            continue
        bucket = classify(text)
        if text not in out[bucket]:
            out[bucket].append(text)
    return out


def unprovisionable(preconditions: list[str] | None) -> list[str]:
    """挑出**无法置备**的前置条件（保序、去重）—— ② 类，用于预检拒跑。"""
    out: list[str] = []
    for item in preconditions or []:
        if is_unprovisionable(item) and str(item) not in out:
            out.append(str(item))
    return out


def provisionable(preconditions: list[str] | None) -> list[str]:
    """挑出**可置备的页面状态**（③ 类）—— 它们应该被展开成导航步骤。"""
    return classify_all(preconditions)[PAGE]


def refusal_message(case_name: str, blocked: list[str]) -> str:
    """拒跑时给用户看的话。**必须可行动**：说清为什么、以及两条出路。"""
    items = "\n".join(f"     · {p}" for p in blocked)
    return (
        f"❌ 拒绝执行「{case_name}」：前置条件 MAT 无法置备\n"
        f"{items}\n"
        "   MAT 的动作集（launch/tap/input/clear/swipe/scroll/back/home/key/wait）里\n"
        "   没有任何一个能改变**系统权限或授权状态**，所以这条用例从冷启动跑起注定不成立\n"
        "   —— 跑下去只会产出一条**假的定位失败**（实测：某资讯类客户端的位置授权弹窗用例）。\n"
        "   两条出路：\n"
        "     ① 先手工把设备置成该状态，再加 --allow-unmet-preconditions 放行本次执行；\n"
        "     ② 或改写用例，让它自己用步骤到达该状态（权限类状态通常做不到，建议删掉该前置）。"
    )
