"""探索测试（实现）：规则驱动自主遍历 + 异常发现。

V1.5.1 页面层级模型（L0/L1/L2…）：
- 每页记录 depth（L0=首页）、parent_key（首次到达来源页）、parent_action（导航动作文案）
  → 任何发现项可回溯"复现路径"（承诺的输出）；
- 深度策略：current_depth ≥ max_depth 时不再点击下钻，改为 back 回浅层——把预算留给
  高价值的浅层页面，防钻牛角尖；
- **分级回退 + 回退前先校验 + 回不去就终止**（抄自 refactored）：
  `back`（≤2 次，每次**校验**真的回到了 App）→ 冷启动；**两级都回不去 ⇒ 终止本轮**并报出原因。
  - 「先校验」的坑：必须比较**回退前 vs 回退后**，**不能**拿某个存起来的页面树去比
    ——refactored 旧版就因此**校验恒真**（`site_crawler.py:717`）；
  - **被系统挡住 ≠ 跳去了别的 App**：权限弹窗（`GrantPermissionsActivity` 等）会挡住 App，
    而 `back` 关不掉它、`app_start` 也被它盖住。实测（2026-10-02）混为一谈的后果是
    **一轮 50 次"离场"、0 步、0 页**，且报告里看不出原因。现在会记成 `system-dialog` finding，
    连续 `MAX_LEFT_STREAK` 次即终止本轮；
- 重复到达已知页取更浅深度（min 更新），父链始终是"首次到达路径"。

图谱沉淀：传入 `index_path` 时——
- **开始时读回 `tried`**（已点过的 `(page_key, bounds)`）→ 本次跳过已知分支，预算只花在新分支
  （把"每次从零"变成"越探越深"，这是收益最大的一项）。
  ⚠️ **`page_key` 跨轮不稳（实测同页稳定性仅 12%），精确键匹配几乎必然失败**，所以另有
  **按信号包含度认页**的兜底（`IDENTITY_MATCH`）——那才是真实 App 上真正生效的那条路径。
  报告里 `reused_tried` 是**读回量**、`skipped_by_history` 才是**真正跳过量**，两者别混；
- **结束时 merge 回图谱**：depth / parent_key / parent_action / tried / alt_entries /
  step_health / app_version / identities 沉淀进 `artifacts/page_index/<pkg>.json`（schema v2）。
- `index_path=None` 时行为与改造前**完全一致**（不读不写），单测与调用方零影响。
- **选点策略可关**：`MAT_HEURISTIC=0` 退回修复前的纯轮转（`candidates[step % len]`）。
  这是**对照组**，用来回答"启发式到底有没有让探索变好"——没有对照的测量等于没有测量
  。报告里的 `heuristic` 字段记录本轮用的是哪种。

与 page_index.crawl 的分工：crawl 建控件索引（无异常检测、保守）；explore 是测试行为
——发现崩溃/白屏/黑屏并产出带证据的报告。安全设计同源：敏感控件黑名单、预算双上限、
执行前自动唤醒解锁、logcat 证据留存。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

from .adb import logcat_clear, logcat_tail
from .driver import app_version  # 单源（T76）：runner 的 run.json 也要记它
from .page_index import (
    describe_structure,
    digest_page,
    identities_from_controls,
    load_index,
    merge_edges,
    merge_pages,
    new_index,
    page_key,
    save_index,
)
from .risk import detect_risk_page  # 单一事实来源（risk.py）；探索用的是它的高精度口径
from .safety import SENSITIVE_WORDS as _SENSITIVE  # 单一事实来源（safety.py）
from .ui_tree import parse_nodes

#: 按页面信号**包含度**认页的阈值（增量探索用）。
#:
#: 为什么不能只靠 `page_key`：`tried` 以 `(page_key, bounds)` 为键，而 `page_key` 跨轮不稳
#: （真实 App 实测同页稳定性仅 12%）⇒ 精确匹配几乎永远失败 ⇒ 「跳过已试分支」静默失效
#: （真机两轮：读回 20 条、真正跳过 0 个）。
#:
#: 阈值依据：真实 UI 树上同页包含度中位 **0.70**、跨页中位 **0.02**（`benchmarks/page_identity`
#: 语料实测）。取 0.5 偏"召回优先"——认不出页的代价是整轮增量失效，比认错页更严重。
IDENTITY_MATCH = 0.5

#: 认页还要求"**明显胜出**"：与最佳分数接近（差距 < 本值）的候选必须彼此也是同一页，
#: 否则弃权。
#:
#: 为什么必须有它：不设间距时，语料上会**错跳 10%+ 的候选**——而错跳是**静默丢覆盖**
#: （那些分支永远不会被点到）。加上间距后实测错跳降到 **1.7%**，跳过率仍保持 **80.4%**。
#: 原理：认错页时，真正的本页通常就在第二名、分数接近 ⇒ 差距很小 ⇒ 自动弃权。
#: 注意：图谱里只有 1 页时不存在"第二名"，间距判据自然不生效，退化为只卡阈值——
#: 那种情况下没有可比较的歧义，属预期行为。
IDENTITY_MARGIN = 0.05

#: **系统级 UI 的包名**——它们出现在前台时**不是"被测 App 跳走了"，而是"被测 App 被系统挡住了"**。
#:
#: 为什么必须分开：混为一谈会让探索**空转烧完整轮预算**，而且报告里看不出原因。
#: 实测（2026-10-02）：一个「是否允许录制音频？」权限弹窗挡在 App 前面，
#: `back` 关不掉它、`app_start` 又被它盖住 ⇒ 一轮 **50 次"离场"、0 步、0 页**。
#: 注意**不要把 `com.android.settings` 列进来**——它本身就是合法的被测目标（spike 的 t0 任务就是它）。
SYSTEM_UI_PREFIXES = (
    "com.android.permissioncontroller",
    "com.android.packageinstaller",
    "com.android.systemui",
)

#: 连续离场/被挡的容忍上限。超过就**终止本轮**并报出原因——继续转下去只是烧预算。
#: 依据同上：实测无上限时会一路转到 50 次。
MAX_LEFT_STREAK = 8

#: "连续多少迭代没发现新页面"就**提前停止**（`MAT_STOP_EXHAUSTED=1` 才启用）。
#:
#: ⚠️ **默认关闭，而且实测证明它不该默认开**：
#: 本意是"探完了就停"以消除"能跑几步取决于设备快慢"的抖动，但**真机实测反而把方差搞大了**——
#: 同配置 4 轮页数 `[9, 24, 9, 12]`（极差 2.67×、CV 111%），而关掉它时是 `[6,6,6,7]`（CV 16%）。
#: 原因：第 2 轮跑到 60 步**仍在发现新页面**（无新页步数=2），说明轮 1/3 的"连续 8 步无新页"
#: 是**误触发**——它们远没探完，只是碰上一段回退/上下行的长间歇。
#: ⇒ **"连续无新页"不是"探完了"的可靠信号**，且它何时触发本身取决于路径。
#:
#: **教训：端到端探索的页数本质是路径依赖的抽样，改停止条件改变不了它的可复现性。**
#: 可复现只能靠：① 修前提（状态文案 / 开关 / 起始状态，已做）；② 多次运行**报分布**；
#: ③ 用"K 次并集"这类更稳的指标。**`stop_reason` 与 `idle_steps` 仍照常记录**——
#: 报告永远有用，但"替用户决定何时停"要用户自己开口。
MAX_IDLE_STEPS = 8

#: 连续多少次**回退**而没有任何一步点击 ⇒ 判定卡住，终止本轮。
#:
#: 为什么必须有（实测踩到）：
#: **回退分支不推进 `step`**，所以 `while step < max_steps` 这个条件**永远到不了**——
#: 一旦可达分支被点完，循环就会一路回退/冷启动直到**时间预算耗尽**（实测有测试因此挂了 5 分钟；
#: 真机上表现为"跑了 5 分钟只走 27 步"）。
#: 这与 `MAX_LEFT_STREAK`（管"被系统挡住"）不同：那个管前台不是目标 App，这个管**页面内无路可走**。
#:
#: 取 12：正常探索里回退是**穿插**的（回退几步就会点到新东西），连续 12 次回退都没有点击
#: 已经是明确的"卡住"信号。
MAX_CONSECUTIVE_BACKOFFS = 12

#: 停止原因——写进报告，让"跑完了"能区分**探完了**与**时间/步数到了**。
STOP_EXHAUSTED = "exhausted"
STOP_BUDGET = "budget"
STOP_STUCK = "stuck"
STOP_BLOCKED = "blocked"

#: `stuck` 的**两种成因**——**必须分开报**，否则看不出"探完了"还是"卡住了"，
#: 而两者的处置完全相反：前者要**调大 `--max-depth`**，后者调大也没用（候选本来就没了）。
#: ⚠️ 旧代码把 `not candidates` 与 `current_depth >= max_depth` **混在同一个分支**里，
#: 于是报告只说"无路可走"——实测 4 个 App 里 3 个以 stuck 结束时，
#: **根本看不出到底该调深度还是该改选点策略**。
STUCK_CAUSE_DEPTH = "depth-limit"        # 触到 --max-depth，不再下钻
STUCK_CAUSE_CANDIDATES = "no-candidates"  # 本页候选耗尽（都试过 / 不可点）

#: `blocked` 的**两种成因**——同样是"处置方向完全相反"的一对。
#: ⚠️ 这两处 `break` 原本**不设 `stop_reason`**，于是落回默认值 `budget`：
#: 实测一轮 `duration_seconds=72.9`（预算 900s）却报"预算耗尽"⇒ **用户会去加预算**，
#: 而真实原因是"App 一直往别的 App 跳、拉不回来"（离场 12/13 步）。同族于 `stuck` 的拆分。
BLOCKED_CAUSE_LEFT_STREAK = "left-streak"        # 连续离场/被挡，拉不回
BLOCKED_CAUSE_FALLBACK_FAILED = "fallback-failed"  # back 与冷启动都回不到目标包
#: **第三种成因**：App **根本没启动起来**（前台一直是桌面/别的 App）。
#: ⚠️ 它与 `left-streak` 混在一起报过：实测（2026-10-07 筛选轮）某 App 整轮
#: **0 步 0 页**，报告却写"离场拉不回"——读起来像"App 一直跳走"，
#: 真相是**冷启动比 2s 等待长，MAT 从没等到它到前台**。处置完全不同：
#: 前者要对付 App 的正常跳转，后者只要**等久一点 / 换启动方式**。
BLOCKED_CAUSE_NEVER_STARTED = "never-started"
#: **第四种成因**：撞上被测 App 的**风控/验证页**（滑块、拼图、图形点选）。
#: 与 `no-candidates` 是两回事：风控页只有 2–7 个可点项，试完就"候选耗尽"，
#: 于是**空转 12 次回退**（每轮 1–4 分钟），报告却只说 `stuck` ——
#: 用户会去调深度/改选点，**方向全错**。实测 4 轮 `no-candidates` **全部**终结在风控页上。
BLOCKED_CAUSE_RISK_CONTROL = "risk-control"

#: 启动被测 App 后的**等待梯度**（秒）。只等一次 2s **不够**——重 App 冷启动可能 5–8s。
#: 每次失败都**重新 `app_start` 并加长等待**，三次都等不到就判定"起不来"并终止本轮。
STARTUP_WAITS = (2.0, 4.0, 6.0)

#: 撞到风控/验证页时的策略（`MAT_RISK_POLICY`）。
#:
#: - `stop`（**默认**）：立即终止本轮并报出。
#: - `wait`：暂停并**轮询**，等人把验证过掉再继续；超时后仍按 `stop` 处理。
#:
#: ## 为什么默认是 `stop`（而不是 `wait`）
#:
#: 1. **探索是无人值守场景**（批量跑批 / 夜里跑 / CI）。挂起等人 = **整批卡死**；
#:    而"终止"只是少探一部分——**图谱与截图都留下了**，下一轮还能续。
#: 2. **挂起会污染耗时指标**：T30 那种"三方对比均耗时"在有随机等待时长后不可比。
#: 3. **失败的两种模式不对称**：终止 ⇒ 用户拿到一份**明确的报告**（知道被风控了、
#:    知道去处理）；挂起 ⇒ 用户**什么都拿不到，还不知道为什么**。
#:    ⇒ **终止是更安全的失败模式**。
#:
#: `wait` 留给**有人值守**的场景（单机调试 / 演示 / 评测跑批）——见 `_wait_human_clear_risk`。
MAT_RISK_POLICY_ENV = "MAT_RISK_POLICY"
MAT_RISK_WAIT_SECONDS_ENV = "MAT_RISK_WAIT_SECONDS"
DEFAULT_RISK_WAIT_SECONDS = 120.0


def risk_policy() -> str:
    """`stop`（默认）或 `wait`。"""
    v = os.environ.get(MAT_RISK_POLICY_ENV, "stop").strip().lower()
    return v if v in {"stop", "wait"} else "stop"


def risk_wait_seconds() -> float:
    """`wait` 策略的最长等待（秒）。解析失败回落到默认值——**绝不因为配置写错就永久挂住**。"""
    try:
        return max(0.0, float(os.environ.get(MAT_RISK_WAIT_SECONDS_ENV, DEFAULT_RISK_WAIT_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_RISK_WAIT_SECONDS



def heuristic_enabled() -> bool:
    """图谱启发式选点是否启用（`MAT_HEURISTIC=0` 关闭）。

    **为什么需要这个开关**：
    「启发式让探索变好了吗」这个问题**没有对照就答不了**。没有开关时只能拿真机数字去比
    文档里记录的旧数字，而 App 状态、风控、设备负载都不同 ⇒ **不可比**。
    有了它，同一台设备、同一个 App、同一天连续跑「开 / 关」两轮才是可比的对照。

    ⚠️ **每次调用都读环境变量**，不要在模块级缓存——那正是 `GEN_JSON_REPAIRS` 踩过的坑
    （import 时冻结 ⇒ `.env` 里设了不生效）。
    """
    return os.environ.get("MAT_HEURISTIC", "1").strip().lower() not in {"0", "false", "no", "off"}


def skip_switches_enabled() -> bool:
    """`MAT_SKIP_SWITCHES=1` 时把**开关类控件**排除出候选（见 `_candidate` 的 docstring）。

    为什么需要：点开关不产生新页面，却会**改设备状态**、从而改掉下一轮运行的前提
    （实测：拨一次蓝牙 ⇒ 下一轮蓝牙页 11→34 个控件 ⇒ 结果 `[10,7,10,7,10,7]` 交替）。
    牺牲"点开关"的覆盖，换**可复现的运行前提**——"每条跑 N 遍报极差"的可复现实验场景需要它。

    ⚠️ 与 `heuristic_enabled` 同样**每次读**，不在模块级缓存（避免重演 `GEN_JSON_REPAIRS`
    那个"import 时冻结 ⇒ `.env` 里设了不生效"的坑）。
    """
    return os.environ.get("MAT_SKIP_SWITCHES", "0").strip().lower() in {"1", "true", "yes", "on"}


def _pick(cands: list[dict], matched_keys: set[str], step: int,
          prior_edge_from: dict[str, list[tuple[str, str]]],
          prior_hits: dict[str, int],
          prior_findings: dict[str, int] | None = None) -> dict:
    """图谱启发式选点（+ 评审 R3 修订 + T19 findings 权重，替代纯轮转）：
    ① 用**认页结果**（matched_keys，非裸 page_key——跨轮重合率仅 12%）取历史出边，
       通向低访问/未见页面的入口加分，全新页最高；
    ② 有文案的控件加分（图片按钮语义不可复用，降权）；
    ③ 同分保持轮转（覆盖多样性不丢）。无图谱/未认出页面时退化为「有文案优先 + 轮转」；
    ④ **通向高风险页的入口加分**（prior_findings：历史在该页发现过崩溃/白屏/离场）——
       把"发现过问题的地方"变成回探重点（自产数据冷启动闭环第一环，无需用户数据）。
       优先级：未见页 > 高风险页 > 已访问普通页。"""
    def score(c: dict) -> int:
        s = 3 if c.get("label") else 0
        tos = [dst for mk in matched_keys
               for act, dst in prior_edge_from.get(mk, []) if act == c.get("label")]
        if tos:
            known = [prior_hits[t2] for t2 in tos if t2 in prior_hits]
            best = min(known) if known else 0     # 边指向图谱外新页 → 视为 0 次访问
            s += max(0, 12 - best)
            if prior_findings and any(prior_findings.get(t2) for t2 in tos):
                s += 10                            # 边直达高风险页：优先回探复现
        return s

    best = max(score(c) for c in cands)
    top = [c for c in cands if score(c) == best]
    return top[step % len(top)]


#: 「近均匀」的灰度极差阈值。
SUSPICIOUS_EXTREMA = 6
#: 近均匀行带至少要占屏高的比例。
#: 取 0.6：靶场白屏页实测约 **0.85**（内容区），而正常页（登录页/列表页）没有这么长的空白带。
SUSPICIOUS_BAND_RATIO = 0.6


def _screen_suspicious(png_bytes: bytes) -> str | None:
    """白屏/黑屏启发：找**最长的「近均匀」行带**，够长就判可疑（返回类型标签，否则 None）。

    ⚠️ **为什么不能看整屏极差**（T76 实测，2026-10-07）：真机截图必然含
    **状态栏 + ActionBar + 导航栏** ⇒ 整屏极差**恒为 255**，**白屏永远检不出**。
    靶场白屏页实测：

    | 区域 | 灰度极差 | 旧判据（整屏 <6） |
    |---|---|---|
    | 整屏 | **255** | ❌ 判不出 |
    | 裁掉上下 7% | 246 | ❌ |
    | 裁掉上下 10% | 224 | ❌ |
    | **中间 60%（内容区）** | **0** | ✅ |

    ⇒ 改成"最长的近均匀行带 ≥ 屏高 `SUSPICIOUS_BAND_RATIO`"，语义正是
    「**内容区一片空白**」，且**不依赖"系统栏有多高"**这种设备差异。
    """
    try:
        from PIL import Image
    except ImportError:  # Pillow 已是主依赖；此处只防"上游变化导致白屏检测静默失效"（评审 L10）
        warnings.warn("Pillow 不可用，白屏/黑屏检测已禁用：pip install pillow",
                      RuntimeWarning, stacklevel=2)
        return None
    try:
        image = Image.open(io.BytesIO(png_bytes)).convert("L")
        # 下采样：判"是否均匀"够用，且**不依赖 numpy**（本项目没有该依赖）。
        # ⚠️ **必须用最近邻**：默认的平滑重采样会把"内容/空白"的**边界行混成渐变**，
        # 而渐变行的极差也可能 < 阈值 ⇒ **伪造出并不存在的均匀带**。
        w, h = 80, 200
        resample = getattr(Image, "Resampling", Image).NEAREST
        px = list(image.resize((w, h), resample=resample).getdata())

        best_run, best_sum = 0, 0.0
        run, run_sum, run_lo, run_hi = 0, 0.0, 1e9, -1e9
        for y in range(h):
            row = px[y * w:(y + 1) * w]
            mean = sum(row) / w
            flat = max(row) - min(row) < SUSPICIOUS_EXTREMA
            # ⚠️ **跨行也必须落在同一片灰度区间**——否则「深色标题栏 + 白色正文」里
            # **每一行都各自均匀**，会被算成一整条带（实测：一条 200/200 的假带）。
            if flat and run and (mean - run_lo >= SUSPICIOUS_EXTREMA
                                 or run_hi - mean >= SUSPICIOUS_EXTREMA):
                run, run_sum, run_lo, run_hi = 0, 0.0, 1e9, -1e9
            if flat:
                run += 1
                run_sum += mean
                run_lo, run_hi = min(run_lo, mean), max(run_hi, mean)
                if run > best_run:
                    best_run, best_sum = run, run_sum
            else:
                run, run_sum, run_lo, run_hi = 0, 0.0, 1e9, -1e9

        if best_run >= h * SUSPICIOUS_BAND_RATIO:
            return "white-screen" if best_sum / best_run > 128 else "black-screen"
    except Exception:
        return None
    return None


def _candidate(xml: str, package: str, skip_switches: bool = False) -> list[dict]:
    """可探索动作 = 被测 App 的可点击节点（黑名单过滤、输入框跳过）。

    **`toggle` 字段**：该候选是不是开关类控件（Switch/CheckBox/ToggleButton）。
    为什么要标出来：**点开关不产生新页面，
    却会改设备状态，从而改掉"下一轮运行的前提"**。实测（系统设置）：探索点了蓝牙开关 ⇒
    下一轮的蓝牙页多出「已配对的设备」列表（11 → 34 个控件）⇒ 页面真的不同 ⇒ 结果 `[10,7,10,7,10,7]`
    严格交替。**这不是签名 bug，是运行前提没归一化**——所以处置是"让它可见/可分层"，不是抹平签名。

    `skip_switches=True` 时**不把开关放进候选**（`MAT_SKIP_SWITCHES=1`）：牺牲"点开关"的覆盖，
    换可复现的运行前提。默认**不跳**——不能悄悄改行为。
    """
    out = []
    for node in parse_nodes(xml):
        if not node.clickable or not node.in_package(package):
            continue
        label = node.label
        if any(word in label for word in _SENSITIVE):
            continue
        if node.cls == "android.widget.EditText":
            continue
        toggle = _is_toggle_class(node.cls)
        if toggle and skip_switches:
            continue
        out.append({"bounds": node.bounds, "label": label, "toggle": toggle})
    return out


def _is_toggle_class(cls: str) -> bool:
    """开关类控件判定（点它 = 改状态，而不是导航）。"""
    c = (cls or "")
    return c.endswith(("Switch", "CheckBox", "ToggleButton", "CompoundButton"))


def _bump_health(health: dict, label: str, ok: bool) -> None:
    """到达动作健康度（step_health）：点击后是否真的换页。"""
    entry = health.setdefault(f"tap|{label}", {"ok": 0, "fail": 0,
                                               "last_ok_at": None, "last_fail_at": None})
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if ok:
        entry["ok"] += 1
        entry["last_ok_at"] = now
    else:
        entry["fail"] += 1
        entry["last_fail_at"] = now


def _identities(xml: str, package: str) -> frozenset[str]:
    """页面信号集合：统一委托 page_index.identities_from_controls（与图谱/认页同源）。

    此前这里有一份独立的 XML 直读实现（含被动 root 节点、rid 优先吞掉文案），
    与 crawl/controls 版在**同一页**上产出不同信号集——跨源认页包含度被稀释、
    并列互相不认（外部评审 P0-3 的深层根因）。单一实现后结构上不可能漂移。
    """
    return identities_from_controls(digest_page(xml, package)["controls"])


def _containment(a: frozenset, b: frozenset) -> float:
    """包含度 = 交集 / 较大集合。`_same_page` 与"认页"共用同一口径，避免两处口径漂移。"""
    return len(a & b) / max(len(a), len(b)) if a and b else 0.0


def is_system_dialog(package: str) -> bool:
    """前台包名是否属于**系统级 UI**（权限弹窗等），而不是"被测 App 跳走了"。"""
    pkg = (package or "").strip()
    return any(pkg.startswith(p) for p in SYSTEM_UI_PREFIXES)


#: 「**同意/允许**」类按钮 —— **默认策略**（2026-10-06 起）。
#:
#: 为什么默认允许：**拒绝会让"需要权限才能进的功能页"永远探不到** ⇒ 覆盖率被自己的策略压住，
#: 量出来的是"策略有多保守"而不是"探索有多强"。定位/相机/通知这类页面在真实 App 里占比不低。
#: 代价：探索会**改设备的授权状态**（也就是"替用户开了权限"），覆盖率不再等于"开箱即用"。
#: ⇒ 要回到"最小干预"就设 `MAT_DIALOG_DENY=1`。
DIALOG_ALLOW_LABELS = ("仅使用期间允许", "允许本次使用", "始终允许", "允许", "同意",
                       "我知道了", "确定", "继续")

#: 「**拒绝/关闭**」类按钮。设 `MAT_DIALOG_DENY=1` 时改用这一组（最小干预，不改授权状态）。
DIALOG_DENY_LABELS = ("禁止", "拒绝", "不允许", "以后再说", "稍后", "暂不", "取消", "关闭")

#: 弹窗按钮的匹配顺序：**先精确、再包含**。
#: ⚠️ 顺序不能反——「允许本次使用」里含「允许」，先做包含匹配会把「始终允许」误选。
_DIALOG_MATCH_EXACT_FIRST = True

#: 做**包含匹配**时要求标签至少这么长。短标签只走精确匹配。
#:
#: ⚠️ 不设这个下限会**点错按钮**：`允许` 是 `不允许` 的子串 ⇒ 想"允许"却点到"**不允许**"；
#: `同意` / `确定` / `取消` 同理容易被长文案包住。这类两字标签只在**整串相等**时才算命中。
_DIALOG_CONTAINS_MIN_LEN = 3


def pick_dialog_button(nodes, allow: bool):
    """从当前树里挑出**该点的那个弹窗按钮**（纯函数，可离线验）。挑不到返回 None。

    ⚠️ **调用前必须确认前台是系统 UI**（`is_system_dialog`）——那时整棵树都属于弹窗，
    否则这里会点到被测 App 自己的「取消」按钮。
    """
    labels = DIALOG_ALLOW_LABELS if allow else DIALOG_DENY_LABELS
    clickable = [n for n in nodes if n.clickable and n.center is not None]
    if _DIALOG_MATCH_EXACT_FIRST:
        for lab in labels:
            for n in clickable:
                if n.label.strip() == lab:
                    return n
    for lab in labels:
        if len(lab) < _DIALOG_CONTAINS_MIN_LEN:
            continue                      # 短标签只精确匹配（见常量注释）
        for n in clickable:
            if lab in n.label:
                return n
    return None


def _dialog_allow() -> bool:
    """弹窗策略：**默认「允许」**；设 `MAT_DIALOG_DENY=1` 回到「最小干预（拒绝）」。

    见 `DIALOG_ALLOW_LABELS` 的注释：默认拒绝会**把"需要权限的功能页"整个探丢**。
    """
    deny = os.environ.get("MAT_DIALOG_DENY", "").strip().lower()
    return deny not in {"1", "true", "yes", "y"}


def dismiss_system_dialog(drv, allow: bool) -> str | None:
    """**主动点掉**挡在前面的系统弹窗，返回被点掉的按钮文案（没点到返回 None）。

    为什么必须做（2026-10-06 实测，T77）：`back` **关不掉**权限弹窗，
    `app_start` 又被它盖住 ⇒ 连续 8 次"留在不了被测 App" ⇒ **提前终止本轮**。
    四个真实 App 里 **3 个**因此被截断（其中一个只探到 13 页、覆盖率从 33% 掉到 7%）。

    **只在确认前台是系统 UI 时调用**（见 `pick_dialog_button` 的说明）。
    """
    try:
        node = pick_dialog_button(parse_nodes(drv.dump()), allow)
    except Exception:            # noqa: BLE001 - 采证类工具：dump 失败不该中断探索
        return None
    if node is None or node.center is None:
        return None
    try:
        drv.click(*node.center)
    except Exception:            # noqa: BLE001 - 同上
        return None
    return node.label.strip()


def foreground_package(drv) -> str:
    """前台包名；取不到返回空串（调用方按"未知"处理，**不要**当成失败）。"""
    return (_app_current(drv).get("package") or "").strip()


def fallback_once(drv, package: str, level: int, sleep_scale: float) -> bool:
    """执行**一级**回退，并**校验是否真的回到了被测 App**。返回是否成功。

    分级（建议抄 refactored 的做法）：

    - `level=1`：`back`（回上一层）
    - `level=2`：`app_start`（冷启动，最重的一级）

    ⚠️ **校验必须比较"回退前 vs 回退后"，不能拿某个存起来的页面树去比**——
    refactored 的旧版就栽在这里（`site_crawler.py:717` 用**父页自己的树**做校验 ⇒ 校验恒真）。
    这里比较的是**前台包名**，语义明确。

    ⚠️ 取不到前台包名时按**成功**处理（不升级）：那只是"不知道"，不该据此判定失败并升级到更重的回退。

    **不抄第三级"按图谱链重导航"**：`arrivals` 里虽有 `_chain_of` / `_replay_once` 可用，
    但对 MAT 的目标（**发现异常**，不是"建可信库"）来说，冷启动已经足够——
    探索不需要保住导航栈（深度会重新推导）。**少一级 = 少一个坏点**，不值得为它引入跨模块耦合。
    """
    try:
        if level <= 1:
            drv.press("back")
        else:
            drv.app_start(package)
    except Exception:
        return False
    time.sleep((1.2 if level <= 1 else 2.5) * sleep_scale)
    pkg_now = foreground_package(drv)
    return (not pkg_now) or pkg_now == package


def _ensure_foreground(drv, package: str, sleep_scale: float = 1.0) -> bool:
    """把被测 App 拉到前台，并**校验它真的上来了**。返回是否成功。

    为什么不能只 `app_start` + 睡 2 秒（旧写法）：**重 App 冷启动可能 5–8s**，
    等待不够时第一帧还是桌面 ⇒ 主循环把它当成"离场"，8 次之后以 `blocked` 终止，
    **整轮 0 步 0 页**。实测（2026-10-07）：某 App、以及一个没有可启动界面的包，
    都是这个表现，而且报告只说"离场拉不回"——**看起来像 App 一直跳走**。

    ⚠️ **两条必须放行的例外**：
    1. **取不到前台包名**（驱动拿不到）——那只是"不知道"，不是"起不来"；
    2. **前台是系统弹窗**——App 其实**在后面跑着**，只是被权限框盖住。
       这时**不能**判"没起来"，要交给主循环按 T77 处理（点掉它 + 记 `system-dialog`）。
       （改动时漏了这条 ⇒ 既有测试 `test_blocked_by_system_dialog_*` 立刻变红，
       因为 `system-dialog` finding 再也走不到。）
    """
    for wait in STARTUP_WAITS:
        try:
            drv.app_start(package)
        except Exception:
            pass
        time.sleep(wait * sleep_scale)
        pkg_now = foreground_package(drv)
        if not pkg_now or pkg_now == package:
            return True
        if is_system_dialog(pkg_now):
            return True
    return False


#: `wait` 策略的轮询间隔（秒）。
RISK_WAIT_POLL_SECONDS = 2.0


def _wait_human_clear_risk(drv, sleep_scale: float = 1.0) -> bool:
    """`wait` 策略：**轮询**等人把风控过掉。返回是否已清除。

    ⚠️ **只能轮询，不能"等回车"**——非交互场景（批量/CI）下等回车会**永久挂住**。
    轮询 + 超时能在两种场景下都安全降级。

    ⚠️ 人工过完验证后 App 落在哪一页是**未知的**（可能回到触发页，也可能回首页）
    ⇒ 调用方**丢弃当前这一帧**、下一轮重新 dump 判定（导航栈会自然重新推导）。
    """
    total = risk_wait_seconds() * sleep_scale
    if total <= 0:
        return False
    deadline = time.monotonic() + total
    print(f"[risk] 撞到风控/验证页；按 {MAT_RISK_POLICY_ENV}=wait **暂停等待人工处理**"
          f"（最多 {risk_wait_seconds():.0f}s，每 {RISK_WAIT_POLL_SECONDS:.0f}s 复查一次）…",
          flush=True)
    while time.monotonic() < deadline:
        time.sleep(RISK_WAIT_POLL_SECONDS * sleep_scale)
        try:
            xml = drv.dump()
        except Exception:
            continue
        if not detect_risk_page(_activity_of(drv), xml):
            return True
    return False


def match_prior_page_ex(ids: frozenset,
                        prior: list[tuple[frozenset, set[str], str]]
                        ) -> tuple[set[str], set[str]]:
    """认页详版：返回 (matched_bounds, matched_prior_keys)。

    matched_prior_keys = 并列候选的 page_key 集合——图谱启发式选点（_pick）靠它
    找到"当前页在历史里的记录"，进而用该记录的**出边**做新页面入口加分
    （外部评审 R3：page_key 跨轮重合率仅 12%，按裸 key 查边等于没用图谱）。
    """
    """按**信号包含度**在图谱里认页，返回该页"上次已试过"的 bounds 集合（认不出则空集）。

    这是增量探索的核心判据，抽成纯函数是为了让**离线语料测试与线上代码走同一份实现**
    （否则测试里再写一遍匹配逻辑，两边一漂移，测试就在验证一个不存在的东西）。

    判据（两条都要满足）：
      1. 最佳匹配的包含度 ≥ `IDENTITY_MATCH`；
      2. 与最佳"分数接近"（差距 < `IDENTITY_MARGIN`）的候选，**要么只有一个**，
         **要么它们彼此也是同一页**（互相包含 ≥ `IDENTITY_MATCH`）。

    第 2 条为什么这么写：图谱里同一页会有**多条重复记录**（`page_key` 不稳，每次观测都建新页；
    真机实测 25 页对应约 12 个真实页），所以"并列同分"是常态。若一见并列就弃权，认页等于没做
    （实测过）。只有当并列的是**真正不同的页**时才弃权——那才是会认错的情形。

    并列时取**交集**而不是并集：并列候选若真是同一页的重复记录，它们的 tried 本就高度重合，
    交集≈并集；若只是巧合撞上（不同页但信号相似），交集往往很小 ⇒ 几乎不跳。
    **宁可少跳，不可错跳**——错跳是静默丢覆盖。
    """
    if not prior or not ids:
        return set(), set()
    scored = sorted(((_containment(ids, k_ids), i) for i, (k_ids, _, _) in enumerate(prior)),
                    reverse=True)
    best_score, _ = scored[0]
    if best_score < IDENTITY_MATCH:
        return set(), set()
    near = [i for score, i in scored if best_score - score < IDENTITY_MARGIN]
    near_ids = [prior[i][0] for i in near]
    same_page = all(_containment(near_ids[0], other) >= IDENTITY_MATCH
                    for other in near_ids[1:])
    if len(near) == 1 or same_page:
        out = set(prior[near[0]][1])
        for i in near[1:]:
            out &= prior[i][1]
        keys = {prior[i][2] for i in near}
        return out, keys
    return set(), set()


def match_prior_page(ids: frozenset,
                     prior: list[tuple[frozenset, set[str]]]) -> set[str]:
    """认页薄封装（语料测试沿用旧签名）：只返回 matched bounds。"""
    detailed = [(k_ids, bounds, "") for k_ids, bounds in prior]
    return match_prior_page_ex(ids, detailed)[0]


def _same_page(prev: frozenset, cur: frozenset, threshold: float = 0.8) -> bool:
    """上一步包含度判定（保守）：交集 / 较大集合 ≥ 阈值 → 判"仍在同页"。

    RN 渐进渲染会让同一页面每帧控件集合抖动（105↔133 个控件交替），
    page_key 全不同 → 图谱膨胀 + 步数原地打转（80 步里 15 步耗在同一店铺页）。
    本函数是 §8 修订顺位 1 的**保守版**：只与上一步比较，不做全局聚类——
    宁可漏杀（错把同页当两页），不可错杀（把真不同的页合并丢页）。
    """
    return _containment(prev, cur) >= threshold


def _path_of(key: str, pages: dict) -> str:
    """回溯父链 → 复现路径文案（"L0 首页 → 点[外卖] → L1 ..."）。"""
    chain: list[str] = []
    seen: set[str] = set()
    cur = key
    while cur is not None and cur not in seen and cur in pages:
        seen.add(cur)
        page = pages[cur]
        prefix = f"L{page['depth']}"
        label = page.get("parent_action") or page.get("title") or page["page_key"][:8]
        chain.append(f"{prefix} {label}" if page["depth"] else f"{prefix} 启动/首页")
        cur = page.get("parent_key")
    return " → ".join(reversed(chain))


def explore(drv, package: str, minutes: float = 2.0, max_steps: int = 30,
            artifacts_dir: Path | None = None, on_event=None,
            max_depth: int = 3, sleep_scale: float = 1.0,
            index_path: Path | None = None) -> dict:
    """规则驱动探索（L0-Ln 层级模型）：返回 {steps, page_count, depth_stats, findings, pages...}。

    深度语义：L0=启动首页；点击一次到达的页=L1；再点一次=L2……
    到达已知页取更浅深度（min 更新）；超过 max_depth 不再下钻，改回退。

    `index_path` 非空时启用 P0 图谱沉淀：开始读回 `tried` 跳过已知分支，结束 merge 回图谱。
    为 None 时与改造前行为完全一致（不读不写）。
    """
    started = time.monotonic()
    deadline = started + minutes * 60
    raw = getattr(drv, "raw", None)
    if raw is not None:
        for action in ("wake", "unlock"):
            try:
                getattr(raw, action)()
            except Exception:
                pass

    # 保证被测 App 在前台再开跑（真机教训：手机停在桌面时直接开跑，整轮预算
    # 耗在"离场 → back（在桌面上无效）→ 再离场"的循环里，0 步 0 页）。
    # ⚠️ **必须校验 + 重试**（T75）：旧写法只 `app_start` + 睡 2s，重 App 冷启动
    # 5–8s 就等不到 ⇒ 主循环把它当"离场"，8 次后终止，整轮 0 步 0 页。
    launch_ok = _ensure_foreground(drv, package, sleep_scale)
    # **每轮清一次 logcat**（T76）：崩溃检测靠 logcat，窗口已开到整段缓冲区
    # ⇒ 上一轮的崩溃会让本轮的无关失败被误判成崩溃。清一次把时效限定在本轮。
    logcat_clear(getattr(drv, "serial", None))

    artifacts_dir = Path(artifacts_dir) if artifacts_dir else Path("artifacts") / "explore" / "tmp"
    (artifacts_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "logs").mkdir(exist_ok=True)

    index_path = Path(index_path) if index_path else None
    prior = load_index(index_path) if index_path else None
    app_version_now = app_version(drv, package)

    findings: list[dict] = []

    def _bump_page_finding(key: str | None, ftype: str) -> None:
        """把 finding 记到页面记录上（pages[key]["findings"][type] 累加）——
        随图谱 merge 落盘，下轮 _pick 据此给高风险页出边加分（T19）。"""
        if key and key in pages:
            bucket = pages[key].setdefault("findings", {})
            bucket[ftype] = bucket.get(ftype, 0) + 1
    pages: dict[str, dict] = {}          # key → 页面记录（含 depth/parent_key/parent_action）
    tried: set[tuple[str, str]] = set()
    tried_by_page: dict[str, list[str]] = {}   # page_key → 本次点过的 bounds（落盘供下次复用）
    step_health: dict[str, dict] = {}          # "tap|<文案>" → {ok, fail, ...}
    observed_edges: list[tuple[str, str, str]] = []  # (父页, 到达动作, 子页) 观察到的跳转
    step = 0
    # 选点策略在**本次运行开始时**定一次（不是模块级缓存）：`MAT_HEURISTIC=0` 走对照组。
    use_heuristic = heuristic_enabled()
    use_skip_switches = skip_switches_enabled()   # MAT_SKIP_SWITCHES=1：不点开关（换可复现前提）
    stop_on_exhausted = os.environ.get("MAT_STOP_EXHAUSTED", "0").strip().lower() \
        in {"1", "true", "yes", "on"}                 # 默认**关**（见 MAX_IDLE_STEPS 的说明）
    idle_steps = 0                                # 连续多少迭代没发现新页面
    backoffs = 0                                  # 连续多少次回退而没有任何点击（防"页面内无路可走"时空转）
    backoff_depth_hits = 0                        # 其中"触到深度上限"造成了几次（见 STUCK_CAUSE_*）
    backoff_cand_hits = 0                         # 其中"候选耗尽"造成了几次
    stop_reason = STOP_BUDGET                     # 缺省按"预算耗尽"结束，被下面改写
    stuck_cause: str | None = None                # 仅 stop_reason == stuck 时有值（见 STUCK_CAUSE_*）
    blocked_cause: str | None = None              # 仅 stop_reason == blocked 时有值（见 BLOCKED_CAUSE_*）
    if not launch_ok:
        # **"根本没起来" 必须与 "跳走了" 分开报**：前者只要等久一点/换启动方式，
        # 后者是对付 App 的正常跳转。混在一起时报文读起来像后者（实测踩到，见 T75）。
        stop_reason = STOP_BLOCKED
        blocked_cause = BLOCKED_CAUSE_NEVER_STARTED
        findings.append({
            "type": "blocked",
            "cause": blocked_cause,
            "detail": f"启动 {package} 后**连续 {len(STARTUP_WAITS)} 次都没等到它到前台**"
                      f"（等待 {STARTUP_WAITS}）——已终止本轮，避免空转烧完预算。"
                      f"常见原因：冷启动比等待更慢 / 该包**没有可启动界面**"
                      f"（`adb shell cmd package resolve-activity --brief {package}` 查一下）/ 设备锁屏"})
    start_page_key: str | None = None             # 本轮**起始页**的 key（供多次运行分层比较）
    toggle_clicks = 0                             # 本轮点了几次**开关类**控件（点它会改设备状态）
    backs = 0
    seen_crash = False
    stack: list[str] = []                # 导航栈（L0 → 当前页的 key 链），深度 = len-1
    pending_nav: dict | None = None      # 刚点击的动作 {"label": ...}，下一轮 dump 结算
    last_key: str | None = None          # 上一个快照的 key（同屏去重用）
    last_ids: frozenset[str] = frozenset()
    dedup_merges = 0                     # 被"仍在同页"门禁合并掉的快照数
    left_app_total = 0                   # 离场总次数（按 activity 去重记 finding，次数汇总进 detail）
    left_app_seen: set[str] = set()
    left_streak = 0                      # 连续离场次数（≥2 说明 back 无效，改 app_start 拉回）

    # P0 增量探索：读回上次已试分支（跳过它们，预算只花在新分支上）
    # 图谱启发式索引（用已有 edges/hits 替代纯轮转，1–2 天零成本）
    prior_edge_from: dict[str, list[tuple[str, str]]] = {}   # from_key → [(action, to_key)]
    prior_hits: dict[str, int] = {}
    prior_findings: dict[str, int] = {}   # T19：page_key → 该页 findings 计数和（高风险页权重）
    if prior:
        for e in prior.get("edges", []) or []:
            src, act, dst = e.get("from", ""), e.get("action", ""), e.get("to", "")
            if src and act:
                prior_edge_from.setdefault(src, []).append((act, dst))
        for pg in prior.get("pages", []) or []:
            k = pg.get("page_key")
            if k:
                try:
                    prior_hits[k] = int(pg.get("hits", 0) or 0)
                except (TypeError, ValueError):
                    prior_hits[k] = 0
        # T19 findings 权重：历史在该页发现过崩溃/白屏/离场 → 下轮优先回探。
        # 与 tried 同一个坑的同一个解法：认页用包含度（matched_keys），不裸比 page_key。
        for pg in prior.get("pages", []) or []:
            k = pg.get("page_key")
            fw = pg.get("findings")
            if k and isinstance(fw, dict):
                prior_findings[k] = sum(v for v in fw.values() if isinstance(v, (int, float)))

    # 从图谱读回上次已试分支。注意区分两件事（2026-10-01 修正）：
    #   `reused_tried`       = **读回**多少条。它只说明"图谱里有历史"，**不说明这些历史起了作用**。
    #   `skipped_by_history` = 本轮**真正因此跳过**多少个候选——这才是增量探索的收益。
    # 两者会差一个数量级：`tried` 存的是 `(page_key, bounds)`，只要 page_key 跨轮不稳
    # （真实 App 上很常见，实测同页稳定性仅 12%），读回的历史就永远匹配不上当前页的 key，
    # 于是"复用了 500 条"实际跳过 0 个。原先 CLI 只报前者并写"（跳过，预算花在新分支）"，
    # 是**不诚实的**——已改。
    reused_tried = 0
    prior_tried: set[tuple[str, str]] = set()
    # 按**页面信号包含度**认页的备选路径（`page_key` 精确匹配失败时的兜底）。
    # 为什么需要：`tried` 以 `(page_key, bounds)` 为键，而 `page_key` 跨轮不稳
    # （真实 App 实测同页稳定性仅 12%）⇒ 精确匹配几乎永远失败 ⇒ 增量探索静默失效。
    # 包含度则能认出"这是同一页"（实测同页中位 0.70 / 跨页 0.02，分离度清晰）。
    prior_by_ids: list[tuple[frozenset, set[str]]] = []
    if prior:
        for known in prior.get("pages", []):
            known_key = known.get("page_key")
            known_bounds = [str(b) for b in (known.get("tried") or [])]
            if known_key:
                for bounds in known_bounds:
                    if (known_key, bounds) not in tried:
                        tried.add((known_key, bounds))
                        prior_tried.add((known_key, bounds))
                        reused_tried += 1
            known_ids = known.get("identities") or []
            if known_ids and known_bounds:
                prior_by_ids.append((frozenset(known_ids), set(known_bounds), known_key))
    skipped_by_history = 0
    # 本轮**检查过**的候选槽位总数（含被跳过的）。为什么要它：`skipped_by_history` 是
    # **按迭代**累加的——同一页停留多轮会把同一批被跳过的候选重复计数（实测：一轮 0 步却报
    # "跳过 27 个"）。所以分子必须有**同口径的分母**才能相除，否则又变成 `reused_tried` 那类
    # 分母错配。两者都按迭代计，比值就无偏且跨轮可比。
    candidates_considered = 0
    identity_matched_pages: set[str] = set()   # 本轮靠包含度（而非 page_key）认出是旧页的页
    risky_matched: set[str] = set()            # 本轮认页命中的"历史高风险页"（T19，报告用）

    def _record(key: str, xml: str, step: int, ids: frozenset) -> dict:
        """登记页面。深度从导航栈推导：pending 点击 = 栈顶+1；back/直达 = 栈顶。

        已知页做 min 深度更新（更浅路径发现时更新父链）。
        """
        if pending_nav and stack:
            depth = pages[stack[-1]]["depth"] + 1
            parent_key, parent_action = stack[-1], pending_nav.get("label")
        elif stack:
            depth = pages[stack[-1]]["depth"]
            parent_key, parent_action = stack[-1], None
        else:
            depth, parent_key, parent_action = 0, None, None
        if key in pages:
            if depth < pages[key]["depth"]:
                pages[key]["depth"] = depth
                pages[key]["parent_key"] = parent_key
                pages[key]["parent_action"] = parent_action
            return pages[key]
        shot_name = f"page_{len(pages):02d}_{key}.png"
        shot_path = artifacts_dir / "screenshots" / shot_name
        try:
            drv.screenshot(str(shot_path))
        except Exception:
            shot_path = None
        pages[key] = {"page_key": key, "depth": depth, "parent_key": parent_key,
                      "parent_action": parent_action, "activity": _activity_of(drv),
                      "step": step,
                      "screenshot": f"screenshots/{shot_name}" if shot_path else None,
                      # 存页面信号集合：下次探索据此按**包含度**认页（见 IDENTITY_MATCH）
                      "identities": sorted(ids),
                      **digest_page(xml, package)}
        if shot_path is not None and shot_path.exists():
            # C4（2026-10-12 评审）：u2 真机上"截图不抛错但文件没写出来"并不罕见——
            # 只捕异常不验存在，read_bytes 会 FileNotFoundError 终止**整轮探索**
            # （此处图谱尚未 merge，此前所有真机预算作废）。白屏检测一并兜 OSError。
            try:
                suspicious = _screen_suspicious(shot_path.read_bytes())
            except OSError:
                suspicious = False
        else:
            shot_path = None
            suspicious = False
            pages[key]["screenshot"] = None  # 文件没写出来就不许在图谱里挂死链
        if suspicious in ("white-screen", "black-screen"):
            findings.append({"type": suspicious,
                             "detail": f"页面疑似{'白' if suspicious == 'white-screen' else '黑'}屏（L{depth}，step={step}）",
                             "screenshot": f"screenshots/{shot_name}",
                             "page_key": key})
            _bump_page_finding(key, suspicious)
        return pages[key]

    while launch_ok and step < max_steps and time.monotonic() < deadline:
        if stop_on_exhausted and idle_steps >= MAX_IDLE_STEPS:
            stop_reason = STOP_EXHAUSTED
            findings.append({
                "type": "exhausted",
                "detail": f"连续 {idle_steps} 步没有发现新页面——判定**已探完**，提前结束"
                          f"（不再空转到预算耗尽；页数因此由可达集合决定，而不是设备快慢）"})
            break
        logcat = logcat_tail(getattr(drv, "serial", None))
        if "FATAL EXCEPTION" in logcat and not seen_crash:
            seen_crash = True
            crash_key = stack[-1] if stack else None
            findings.append({"type": "app-crash",
                             "detail": "logcat 检出 FATAL EXCEPTION（崩溃），建议结合截图定位触发步骤",
                             "page_key": crash_key})
            _bump_page_finding(crash_key, "app-crash")
            (artifacts_dir / "logs" / "crash.logcat.txt").write_text(logcat, encoding="utf-8")

        try:
            xml = drv.dump()
        except Exception as exc:
            findings.append({"type": "env-issue", "detail": f"控件树获取失败: {exc}"})
            time.sleep(1.5 * sleep_scale)
            continue

        # 离场检测：点击跳到了第三方 App（如扫码 SDK 唤起外部应用）→ 记录并回退，
        # 不纳入被测 App 的页面层级（真机自测教训：外部应用页面曾被记为 L0）。
        # 按**前台 package** 判定——activity 类名与 App 包名可以不同命名空间
        # （真机坑：某 RN 应用 activity 在 com.xxx.android.pt.*，包名却是另一个域名前缀，
        # 按 activity 前缀判会把自己当离场，整轮空转）。
        # back 一次无效（如落在系统桌面）时改用 app_start 拉回。
        current = _app_current(drv)
        pkg_now = (current.get("package") or "").strip()
        activity = (current.get("activity") or "").strip()
        if pkg_now and pkg_now != package:
            where = f"{pkg_now} / {activity[:40]}".strip(" /")
            left_app_total += 1
            left_streak += 1
            # **区分"被系统挡住"与"跳去了别的 App"**：前者 back 关不掉、app_start 也会被盖住，
            # 混为一谈会让探索空转（实测：权限弹窗挡路 ⇒ 一轮 50 次"离场"、0 步）。
            blocked = is_system_dialog(pkg_now)
            # **主动点掉系统弹窗**（T77）：`back` 关不掉权限框、`app_start` 又被它盖住，
            # 实测会连续 8 次空转到终止（4 个真实 App 里 3 个被截断）。
            # 策略默认「允许」——拒绝会把"需要权限的功能页"整个探丢（见 DIALOG_ALLOW_LABELS）。
            # 只在 `blocked` 为真时点 —— 那时整棵树都属于那个弹窗，不会误点被测 App。
            dismissed = dismiss_system_dialog(drv, _dialog_allow()) if blocked else None
            if where not in left_app_seen:
                left_app_seen.add(where)
                if blocked:
                    detail = (f"被测 App 被**系统弹窗**挡住（{where[:60]}）——不是 App 自己跳走。")
                    if dismissed:
                        detail += f"已**自动点掉「{dismissed}」**。"
                    else:
                        detail += ("**没找到可点的按钮**（`back` 通常也关不掉这类弹窗）"
                                   "⇒ 需人工处理（授权/关闭）或改用 `--serial` 换设备")
                    findings.append({"type": "system-dialog", "detail": detail})
                else:
                    left_key = stack[-1] if stack else None
                    findings.append({"type": "left-app",
                                     "detail": f"探索跳出了被测 App（当前 {where[:60]}），已自动拉回。"
                                               f"若为正常跳转请人工确认",
                                     "page_key": left_key})
                    _bump_page_finding(left_key, "left-app")
            if left_streak >= MAX_LEFT_STREAK:
                # ⚠️ 必须设 `stop_reason`：否则落回默认 `budget` ⇒ 用户会去加预算，
                # 而真实原因是"拉不回被测 App"（见 BLOCKED_CAUSE_* 的注释）。
                stop_reason = STOP_BLOCKED
                blocked_cause = BLOCKED_CAUSE_LEFT_STREAK
                findings.append({
                    "type": "blocked",
                    "cause": blocked_cause,
                    "detail": f"连续 {left_streak} 次无法留在被测 App"
                              f"（{'系统弹窗未清除' if blocked else '离场拉不回'}）——"
                              f"已**终止本轮**，避免继续空转烧完预算"})
                break
            # 拉回：**点掉弹窗本身就是最有效的一步**（点完前台会回到 App），
            # 只有没点掉时才退回 back / app_start。
            if dismissed is None:
                if left_streak >= 2:
                    try:
                        drv.app_start(package)
                    except Exception:
                        pass
                else:
                    try:
                        drv.press("back")
                    except Exception:
                        pass
            if len(stack) > 1:
                stack.pop()
            pending_nav = None
            backs += 1
            time.sleep(1.5 * sleep_scale)
            continue
        left_streak = 0

        key = page_key(xml, package)
        # 同屏去重（保守版）：key 变了但与上一步包含度 ≥0.8 →
        # 判"仍在同页"（MRN 渐进渲染/图片轮播抖动），沿用上一步的 key——
        # 不新增页面、不推进深度；后续 tried 过滤自然换点下一个未试元素。
        ids = _identities(xml, package)
        if key != last_key and _same_page(last_ids, ids):
            dedup_merges += 1
            key = last_key
        last_key, last_ids = key, ids
        pages_before = len(pages)
        page = _record(key, xml, step, ids)
        if start_page_key is None:
            start_page_key = key
        idle_steps = 0 if len(pages) > pages_before else idle_steps + 1
        current_depth = page["depth"]

        # **风控页：立即终止本轮**（T74）。实测 4 轮 `no-candidates` **全部**终结在风控页上
        # （`JDRiskHandleActivity`、`YodaRouterTransparentActivity` / 滑块验证）——
        # 它们只有 2–7 个可点项，试完就"候选耗尽"，然后**空转 12 次回退**（每轮 1–4 分钟）。
        # 而报告只写 `stuck`，用户会去调深度/改选点，**方向全错**。
        # 页面**照常登记**（截图 + 图谱），这样报告里能看到到底卡在哪一页。
        risk = detect_risk_page(_activity_of(drv), xml)
        if risk and risk_policy() == "wait" and _wait_human_clear_risk(drv, sleep_scale):
            # 人工过完验证 ⇒ **继续本轮**。当前这一帧作废（App 落在哪页未知），
            # `pending_nav` 一并清掉，免得把"上一次点击"记到下一页头上。
            findings.append({
                "type": "risk-cleared",
                "page_key": key,
                "detail": f"撞到风控/验证页（命中 `{risk}`），**人工处理已完成 ⇒ 继续本轮探索**。"})
            risk = None
            pending_nav = None
            continue
        if risk:
            stop_reason = STOP_BLOCKED
            blocked_cause = BLOCKED_CAUSE_RISK_CONTROL
            findings.append({
                "type": "blocked",
                "cause": blocked_cause,
                "page_key": key,
                "detail": f"遇到被测 App 的**风控/验证页**（命中 `{risk}`）——已**立即终止本轮**，"
                          f"不继续空转烧预算。⚠️ 这是 App 的**反自动化拦截**，"
                          f"**不是产品缺陷、也不是探索策略的问题**；"
                          f"换时间/换网络/人工过验证后再跑，或把它排除出对照。"
                          f"（想让人工处理完自动接着跑：`{MAT_RISK_POLICY_ENV}=wait`）"})
            break

        # 认页（增量探索用）：先按 page_key 精确匹配（原逻辑，快且零风险），
        # 失配时按**信号包含度**认页 —— 后者才是在真实 App 上真正生效的那条。
        # 判据与"并列候选怎么处理"都收在 `match_prior_page` 里（纯函数，离线语料测试同源）。
        matched_bounds, matched_prior_keys = match_prior_page_ex(ids, prior_by_ids)
        if matched_bounds:
            identity_matched_pages.add(key)

        # 导航结算：首页入栈；点击后落到新页入栈，落到已知页截断栈（返回/跨链接）
        # 结算前先记到达动作健康度：换了页 = ok，没换 = fail（点了没反应）
        if pending_nav:
            label = pending_nav.get("label") or ""
            prev_top = stack[-1] if stack else None
            moved = key != prev_top
            _bump_health(step_health, label, ok=moved)
            if moved and prev_top:
                observed_edges.append((prev_top, label, key))
        if not stack:
            stack.append(key)
        elif pending_nav and key != stack[-1]:
            if key not in stack:
                stack.append(key)
            else:
                del stack[stack.index(key) + 1:]
        pending_nav = None

        candidates = []
        for a in _candidate(xml, package, skip_switches=use_skip_switches):
            candidates_considered += 1
            t = (key, a["bounds"])
            if t in tried:
                if t in prior_tried:
                    skipped_by_history += 1   # 精确键命中（原路径）
                continue
            if a["bounds"] in matched_bounds:
                skipped_by_history += 1       # 按包含度认页命中（新路径）
                continue
            candidates.append(a)
        # 深度策略：达到 max_depth 不再下钻（点击必然 +1 层），改回退。
        # **分级回退**：`back`（≤2 次，每次**校验**真的回到了 App）→ 冷启动；
        # **两级都回不去就终止本轮**——继续空转只是烧预算，而且报告里看不出原因
        # （实测：权限弹窗挡路时一轮 50 次"离场"、0 步）。
        at_depth_limit = current_depth >= max_depth
        if not candidates or at_depth_limit:
            backoffs += 1
            # **分开计数**：两者处置相反（见 STUCK_CAUSE_* 的注释）
            if at_depth_limit:
                backoff_depth_hits += 1
            else:
                backoff_cand_hits += 1
            if backoffs >= MAX_CONSECUTIVE_BACKOFFS:
                stop_reason = STOP_STUCK
                stuck_cause = (STUCK_CAUSE_DEPTH if backoff_depth_hits >= backoff_cand_hits
                               else STUCK_CAUSE_CANDIDATES)
                if stuck_cause == STUCK_CAUSE_DEPTH:
                    hint = (f"**触到深度上限 L{max_depth}** —— 调大 `--max-depth` 才有用")
                else:
                    hint = "**候选耗尽**（本页能点的都试过了）—— 调大深度**也没用**，要改选点策略"
                findings.append({
                    "type": "stuck",
                    "cause": stuck_cause,
                    "detail": f"连续 {backoffs} 次回退都没能点出任何一步 ⇒ 判定本轮无路可走，终止。"
                              f"回退序列成因：**触到深度上限** {backoff_depth_hits} 次 / "
                              f"**候选耗尽** {backoff_cand_hits} 次 ⇒ 主因是 {hint}。"
                              f"（回退不推进步数，`max_steps` 永远到不了，所以必须显式终止）"})
                break
            level = 1 if backs < 2 else 2
            # ⚠️ **失败必须"升级"到下一级**（T79）：旧写法在这里**直接终止**，
            # 而 `level` 只在**成功**时推进 ⇒ **冷启动那一级从来没被试过**，
            # 报文却写着"back 与冷启动都回不去"——**说了没发生的事**（同族于 T69/T73/T75）。
            # 实测后果：在**根 Activity** 上 `back` 会**退出 App**（前台变桌面）
            # ⇒ 明明冷启动能救回来，却直接判死，整轮 `blocked`。
            if not fallback_once(drv, package, level, sleep_scale):
                if level == 1 and fallback_once(drv, package, 2, sleep_scale):
                    level = 2                      # 升级成功：按冷启动处理导航栈
                else:
                    stop_reason = STOP_BLOCKED
                    blocked_cause = BLOCKED_CAUSE_FALLBACK_FAILED
                    findings.append({
                        "type": "blocked",
                        "cause": blocked_cause,
                        "detail": f"回退失败（back 与冷启动都回不到 {package}）——"
                                  f"已**终止本轮**，避免空转烧完预算。常见原因：系统弹窗未清除 / "
                                  f"App 被系统限制后台 / 设备锁屏"})
                    break
            if level == 1:
                backs += 1
                if len(stack) > 1:
                    stack.pop()
            else:  # 冷启动：导航栈作废，只保留首页
                backs = 0
                stack = [k for k in stack if k in pages and pages[k]["depth"] == 0] or []
            continue

        # R3：用**认页结果**取历史出边——page_key 跨轮重合率仅 12%，按裸 key 查边等于没用图谱
        # 启发式选点；`MAT_HEURISTIC=0` 时退回**修复前的纯轮转**——那是对照组，
        # 不是"旧代码残留"（见 heuristic_enabled 的 docstring）。
        if use_heuristic:
            target = _pick(candidates, matched_prior_keys, step, prior_edge_from, prior_hits,
                           prior_findings)
            risky_matched |= {k for k in matched_prior_keys if prior_findings.get(k)}
        else:
            target = candidates[step % len(candidates)]
        backoffs = 0                              # 点出一步 ⇒ 没卡住
        backoff_depth_hits = backoff_cand_hits = 0  # 成因计数只在**连续**回退序列里有意义
        if target.get("toggle"):
            toggle_clicks += 1
        tried.add((key, target["bounds"]))
        tried_by_page.setdefault(key, []).append(target["bounds"])
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", target["bounds"])
        if match:
            x1, y1, x2, y2 = (int(g) for g in match.groups())
            try:
                drv.click((x1 + x2) // 2, (y1 + y2) // 2)
                pending_nav = {"label": target["label"]}
            except Exception as exc:
                findings.append({"type": "env-issue", "detail": f"点击失败: {exc}"})
        step += 1
        backs = 0
        time.sleep(1.5 * sleep_scale)
        if on_event:
            on_event(step, max_steps)

    depth_stats: dict[str, int] = {}
    for page in pages.values():
        depth_stats[f"L{page['depth']}"] = depth_stats.get(f"L{page['depth']}", 0) + 1
    if left_app_total:  # 离场发现项带上总次数（循环离场只记一次 activity，不刷屏）
        for f in findings:
            if f.get("type") == "left-app":
                f["detail"] += f"（本轮共离场 {left_app_total} 次）"
    for f in findings: # 复现路径（探索发现附复现步骤）
        page_key_hit = f.get("page_key")
        if page_key_hit and page_key_hit in pages:
            f["path"] = _path_of(page_key_hit, pages)
            f["depth"] = pages[page_key_hit]["depth"]

    # P0 图谱字段收口：每页带上 tried / alt_entries / step_health / app_version
    for page_key_value, page in pages.items():
        if tried_by_page.get(page_key_value):
            page["tried"] = sorted(set(tried_by_page[page_key_value]))
        page["app_version"] = app_version_now
        page["structure"] = describe_structure(page.get("controls") or [])  # P1：零 AI 结构
        # 该页的到达动作健康度（与参考数据的 "tap|搜索框" 同构）
        arrival_key = f"tap|{page.get('parent_action')}" if page.get("parent_action") else None
        page["step_health"] = ({arrival_key: step_health[arrival_key]}
                               if arrival_key and arrival_key in step_health else {})
    # alt_entries：观察到的多入口（排除已作为最短父链的那条）
    for parent_key, action, child_key in observed_edges:
        child = pages.get(child_key)
        if not child:
            continue
        if (parent_key, action) == (child.get("parent_key"), child.get("parent_action")):
            continue
        child.setdefault("alt_entries", []).append({"parent_key": parent_key,
                                                    "parent_action": action})
    # 导航边（完整跳转图，含计数）——与 parent_key 的最短树互补
    edge_counts: dict[tuple, int] = {}
    for parent_key, action, child_key in observed_edges:
        edge_counts[(parent_key, action, child_key)] = \
            edge_counts.get((parent_key, action, child_key), 0) + 1
    edges = [{"from": src, "to": dst, "action": action, "count": count}
             for (src, action, dst), count in sorted(edge_counts.items())]

    # P0 落盘：merge 回页面图谱（跨会话累积层），explore.json 仍是本次报告
    index_target = None
    if index_path is not None:
        index = prior if prior else new_index(package)
        # T45：把页面截图**拷进图谱目录**并改写为图谱相对路径。
        # 此前存 run-relative（散在各次 run 目录）⇒ 以图谱为基准 0/126 可解析，
        # 报告展示页面截图/失败截图对比等一切下游用途都拿不到图。
        graph_shots = index_path.parent / "screenshots" / package
        try:
            graph_shots.mkdir(parents=True, exist_ok=True)
        except OSError:
            graph_shots = None
        merged_pages = []
        for page in pages.values():
            q = dict(page)
            shot = page.get("screenshot")
            if shot and graph_shots is not None:
                src = artifacts_dir / shot
                if src.exists():
                    dest = graph_shots / Path(shot).name
                    try:
                        shutil.copy2(src, dest)
                        q["screenshot"] = f"screenshots/{package}/{Path(shot).name}"
                    except OSError:
                        pass  # 拷贝失败保留原路径（宁可存疑不可谎报）
            merged_pages.append(q)
        merge_pages(index, merged_pages, source="explore")
        merge_edges(index, edges)
        save_index(index_path, index)
        index_target = str(index_path)

    report = {
        "explore_id": artifacts_dir.name,
        "package": package,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "steps": step,
        "duration_seconds": round(time.monotonic() - started, 1),
        "page_count": len(pages),
        "depth_stats": depth_stats,
        "max_depth": max_depth,
        "app_version": app_version_now,
        "reused_tried": reused_tried,                       # 从图谱**读回**的已试分支数（≠ 起了作用）
        "skipped_by_history": skipped_by_history,           # 本轮**真正**因历史而跳过的候选数（增量探索的真实收益）
        "candidates_considered": candidates_considered,     # 本轮检查过的候选槽位总数（skip_rate 的分母，同口径）
        "skip_rate": (round(skipped_by_history / candidates_considered, 3)
                      if candidates_considered else None),  # 增量收益的**正确口径**：跳过 / 检查过
        "heuristic": use_heuristic,                         # 本轮选点是否用了图谱启发式（MAT_HEURISTIC=0 时 False）
        "skip_switches": use_skip_switches,                # 是否跳过了开关类控件（MAT_SKIP_SWITCHES=1）
        "start_page_key": start_page_key,                  # 本轮**起始页** key：多次运行的极差里可能混了"前提不同"
        "stop_reason": stop_reason,                         # exhausted（探完了）/ budget（时间或步数到）/ stuck（无路可走）
        "stuck_cause": (stuck_cause if stop_reason == STOP_STUCK else None),
        # stuck 的**成因**：depth-limit（调大 --max-depth 才有用）/ no-candidates（调大也没用）
        "blocked_cause": (blocked_cause if stop_reason == STOP_BLOCKED else None),
        # blocked 的**成因**：left-streak（App 一直往别处跳）/ fallback-failed（回不去了）
        "stop_on_exhausted": stop_on_exhausted,             # 是否启用了"探完即停"（默认关，见 MAX_IDLE_STEPS）
        # 撞到风控页时的策略（stop / wait）——**配置是口径的一部分**：
        # `wait` 会引入不确定的等待时长，耗时类指标不可与 `stop` 并列（P5）。
        "risk_policy": risk_policy(),
        "idle_steps": idle_steps,                           # 结束时"连续无新页"的步数（判断是否接近探完）
        "backoffs": backoffs,                               # 结束时连续回退次数（≥ 上限说明是"卡住"结束的）"
        "toggle_clicks": toggle_clicks,                    # 点了几次开关（>0 说明本轮**改了设备状态**，下轮前提会变）
        "identity_matched_pages": len(identity_matched_pages),  # 靠包含度认出的旧页数（精确 key 认不出时的兜底）
        "risky_pages": sorted(risky_matched | {f["page_key"] for f in findings if f.get("page_key")}),
        # 高风险页（T19）：本轮认页命中的历史 findings 页 + 本轮实际发生 finding 的页——
        # 下轮 _pick 对通向这些页的入口加分（回探复现），报告侧可据此标"重点回归"
        "dedup_merges": dedup_merges,                       # 同屏抖动快照被合并数
        "left_app": left_app_total,                         # 离场次数（back/app_start 自动拉回）
        "prior_pages": len(prior.get("pages", [])) if prior else 0,
        "index_path": index_target,                         # 合并进的图谱文件（None = 未启用沉淀）
        "edges": edges,                                     # P1：本次观察到的跳转（含计数）
        "pages": list(pages.values()),
        "findings": findings,
    }
    (artifacts_dir / "explore.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _app_current(drv) -> dict:
    """前台 {package, activity}；取不到返回空 dict（调用方按"未知"放行）。"""
    try:
        return drv.foreground_app()
    except Exception:
        return {}


def _activity_of(drv) -> str:
    return _app_current(drv).get("activity", "") or ""
