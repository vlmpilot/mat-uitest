"""步骤执行流水线（体验版 v0.2）：
pre-wait(界面空闲判定) → locate(策略链/自愈) → act → post-verify(重试) → 留痕(截图+logcat) → 断言 → 失败分类。

体验版子集：
- 自愈 v0 = 本地模糊重定位 + 提案记录（不调云端、不自动回写，见 locator.heal_fuzzy）
- 失败五分类 v0 规则引擎：app-crash / env-failure / assert-fail / locate-fail / unknown（规则优先，LLM 归因后续接轻云）
- 方差控制依赖 wait_stable（控件树哈希连续一致判定空闲，替代固定 sleep）
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .dsl import Action, AssertionType, Locator, LocatorStrategy, OnFailure, TestCase, dump_dsl
from .locator import Element, LocateFailed, heal_by_hints, heal_fuzzy, resolve, try_resolve
from .off_page import off_page_kind
from .perf import am_start_w, mem_pss_mb
from .state import state_home as _state_home
from .ui_tree import Node, parse_nodes, rect


# 可注入的暂停（外部评审 P1-7）：测试替换为 no-op，全量测试 <90s
def _cold_start_enabled(params: dict) -> bool:
    """`launch` 是否先强制停止（冷启动）。

    **默认开**（2026-10-21 起）。原因：`app_start` 对后台任务是**热恢复**，上一个用例
    遗留的深层页会被当成 launch 落点——真机实测某生活服务 App 3 条同代用例 0/3，失败原因全是
    "起点是上一轮遗留的搜索结果页"（截图里搜索框还留着上一轮的词），而报错只说
    "定位失败"，把人引向用例/图谱排查。

    回归测试的前提是**起点确定**；热恢复把这个前提交给了上一个用例。

    优先级：步骤参数 `cold`（true/false 显式指定）> 环境变量 `MAT_LAUNCH_COLD` > 默认 true。
    """
    value = params.get("cold")
    if value is None:
        value = os.environ.get("MAT_LAUNCH_COLD", "1")
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _pause(seconds: float) -> None:
    time.sleep(seconds)

# 自愈动作后的观察窗口。先等 0.3s（快界面够用），若控件树纹丝不动再多等这一段复测——
# 0.3s 对慢界面偏短，直接判死会把"只是没来得及变"误判成"点空了"。
SELF_HEAL_SETTLE_EXTRA_SECONDS = 0.7

# 画面指纹的采样尺寸（宽×高）。**为什么除了控件树还要看画面**：自绘界面（canvas /
# WebView / Flutter / 游戏引擎）的内容变化不进入 accessibility 树，于是"树哈希没变"
# 在视觉兜底最该起作用的场景里恰好不成立。2026-10-27 真机复现：canvas 按钮点完后
# 画面明显变绿、文字变成「已点击」，控件树却纹丝不动 ⇒ 只按树判就把成功判成点空。
FRAME_SAMPLE = (36, 80)
# 判定「画面变了」的像素差异比例阈值。指纹 36×80=2880 格，0.5% ≈ 14 格；单格雷差 >8
# 才算数（避开 PNG 压缩噪声与抗锯齿），阈值取低了会让状态栏时钟抖动冒充"画面变了"。
FRAME_DIFF_RATIO = 0.005
FRAME_DIFF_GRAY = 8


def _frame_signature(drv) -> bytes | None:
    """内容区的粗粒度灰度指纹；失败返回 None（调用方按"无法判断"处理）。

    裁掉顶部 5% 与底部 7%：状态栏时钟、电量、系统导航栏的变化与页面内容无关，
    留在指纹里会把"什么都没发生"读成"画面动过"。
    """
    try:
        from PIL import Image
    except ImportError:  # Pillow 是主依赖；缺了就退回只看控件树
        return None
    try:
        image = drv.screenshot_pil()
        if image is None:
            return None
        width, height = image.size
        top, bottom = int(height * 0.05), int(height * 0.93)
        if bottom <= top:
            return None
        return image.crop((0, top, width, bottom)).convert("L").resize(
            FRAME_SAMPLE, Image.BILINEAR).tobytes()
    except Exception:  # 截图失败不能影响判定，交给调用方按 None 处理
        return None


def _frame_changed(before: bytes | None, after: bytes | None) -> bool | None:
    """画面是否真的变了：True=变了 / False=没变 / None=无法判断（截图不可用）。"""
    if not before or not after or len(before) != len(after):
        return None
    # strict=True：长度在上面已校验相等，静默截断会让"半张图变了"被读成整体比例，
    # 反而更容易误判；宁可让它抛，也不要悄悄对齐。
    changed = sum(1 for a, b in zip(before, after, strict=True) if abs(a - b) > FRAME_DIFF_GRAY)
    return (changed / len(before)) > FRAME_DIFF_RATIO

_VAR_RE = re.compile(r"\$\{([^}]+)\}")

_INTERACTIVE = {
    Action.TAP, Action.LONG_PRESS, Action.INPUT, Action.CLEAR, Action.SWIPE, Action.SCROLL, Action.BACK
}
# 输入类动作**不自动重放**。文本框内容变化常常不改控件树结构，
# "无变化 ⇒ 重放一次"对 input/clear 是错误推断——实测文本会被打两遍（追加/覆盖成脏数据）。
_REPLAYABLE = _INTERACTIVE - {Action.INPUT, Action.CLEAR}

_ENV_MARKERS = (
    "device offline", "not connected", "connection", "adb", "usb",
    "uiautomator", "timeout", "timed out", "gateway", "socket",
    # 导航链守卫（2026-10-25）：launch 后前台不是目标 App。归 env-failure 而不是
    # locate-fail —— 它是"设备被别的界面占着"，不是"控件找不到"（后者的下一步症状）。
    "前台不是目标",
)

CATEGORIES = ("app-crash", "env-failure", "assert-fail", "locate-fail", "api", "unknown")


def substitute_vars(text: str, variables: dict[str, str]) -> str:
    """替换 ${变量名}；未知变量原样保留并交由执行时报错。"""
    return _VAR_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), text)


def wait_stable(drv, timeout_ms: int = 4000, interval: float = 0.3) -> bool:
    """界面空闲判定：连续两次控件树哈希一致视为稳定。超时返回 False（继续执行，不失败）。"""
    stable, _ = wait_stable_xml(drv, timeout_ms, interval)
    return stable


def wait_stable_xml(drv, timeout_ms: int = 4000, interval: float = 0.3) -> tuple[bool, str]:
    """wait_stable 的带 XML 版本：调用方可复用最后一次 dump，避免重复获取。"""
    last = drv.dump()
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _pause(interval)
        current = drv.dump()
        if current == last:
            return True, current
        last = current
    return False, last


# -- 启动噪声（开屏广告 / 残留弹窗）--------------------------------------------
#
# 两张标签表的语义**不同**，不能合成一张（合成就会把「精确匹配」的护栏丢掉）：
# ① 权限/确认弹窗的**否定向**按钮 —— 精确 text 匹配。contains 会让「取消」打中
#    「确定取消」这类**肯定向**按钮，点下去就真取消了业务操作（v8 case 实测踩过）；
# ② 开屏广告跳过 —— textContains。真机文案常带倒计时（「跳过 3」「跳过广告 5」）。
_PERM_DENY_LABELS = ("不允许", "Don't Allow", "暂不", "以后再说", "好", "OK")
_SKIP_LABELS = ("跳过", "跳过广告", "关闭", "以后再说")

#: 广告线索词 + 「标签稀疏」阈值：只在这两个信号成立时才为**晚到的广告**多看几眼。
#: 正常业务页两个都不满足 ⇒ 一次即止，不给每条用例加固定等待。
_AD_HINTS = ("互动广告", "开屏广告", "跳过广告", "跳过")
_SPLASH_SPARSE_LABELS = 8

#: 时间预算（2026-10-25 定，实测见 `MAT-痛点状态-优化后` 报告）：
#: 首探 0.6s → 最多 3 轮、每轮间隔 0.8s。最坏情况（一直稀疏且始终没广告）
#: ≈ 0.6 + 2×0.8 + 3 次 dump ≈ 3s；最常见情况（业务页、无广告）只花 1 次 dump。
#: 改动前固定成本 14.0–14.4s/次（1.5s + 10 次 find 各吃满超时 + 1s）。
_SPLASH_SETTLE = 0.6
_SPLASH_RETRY_ATTEMPTS = 3
_SPLASH_RETRY_PAUSE = 0.8
_SPLASH_TAIL = 0.6


def _parse_nodes_safe(xml: str) -> list[Node]:
    """dump 文本 → 节点表；空串或结构异常一律返回 `[]`。

    `parse_nodes` 对**没有任何属性**的 `<node>` 会抛 AttributeError（正则扁平解析的
    边界），而噪声清理是 best-effort：解析不出来等同「没找到」，不能把异常抛给用例。
    """
    if not xml:
        return []
    try:
        return parse_nodes(xml)
    except Exception:  # noqa: BLE001 —— 解析不了就当没找到
        return []


def _pick_label(nodes: list[Node], labels: tuple[str, ...], contains: bool) -> Node | None:
    """按 `labels` 的**优先级顺序**挑第一个命中节点（纯函数，零设备往返）。

    顺序即优先级：先「跳过」后「关闭」—— 反过来会把信息流广告的关闭叉当跳过按钮，
    某些 App 的「关闭」点下去是「不感兴趣」二级弹层，反而多一层噪声。
    """
    for label in labels:
        for node in nodes:
            text = node.text or ""
            hit = label in text if contains else text == label
            if hit:
                return node
    return None


def _splash_likely(nodes: list[Node]) -> bool:
    """这一屏像不像开屏/加载态（决定要不要为晚到的广告再看几眼）。"""
    texts = {node.text for node in nodes} - {""}
    if any(hint in text for text in texts for hint in _AD_HINTS):
        return True
    # 0 个标签**不算**稀疏：多半是 dump 拿到空白页，继续等只是白等
    return 0 < len(texts) < _SPLASH_SPARSE_LABELS


def classify_failure(error: str = "", logcat: str = "", rules: list[dict] | None = None) -> str:
    """失败五分类 v0（本地规则子集）：硬证据优先，日志崩溃标记最优先。

    `rules`（可选）：**人工校准得出的规则**（`<工作区>/calibration/rules.json`，见
    `calibration.py`）。命中即覆盖内置判定 —— 这就是「校准一次，下次不再犯」的那一环。

    ⚠️ 规则**只改分类，不改判定**：它不会让一个失败的步骤变成通过。
    ⚠️ 缺省 `None` ⇒ 完全走内置规则（零行为变化）。
    """
    base = _classify_builtin(error, logcat)
    if not rules:
        return base
    from .calibration import apply_rules  # 局部导入：避免 runner ↔ calibration 循环
    return apply_rules(base, error, logcat, rules)[0]


def _classify_builtin(error: str = "", logcat: str = "") -> str:
    """内置五分类（人工规则之外的原始判定）。"""
    log = (logcat or "").lower()
    # Android: FATAL EXCEPTION / ANR；iOS syslog: EXC_CRASH / 未捕获异常终止。
    # 只用**specific**关键词：iOS 日志里 "fatal error" 太泛，会把普通错误误判成崩溃。
    if ("fatal exception" in log or "anr in " in log
            or "exc_crash" in log or "terminating due to uncaught exception" in log):
        return "app-crash"

    err = error or ""
    if err.startswith("AssertionError") or "断言失败" in err:
        return "assert-fail"
    if "LocateFailed" in err or "定位失败" in err:
        return "locate-fail"

    low = err.lower()
    if any(marker in low for marker in _ENV_MARKERS):
        return "env-failure"
    return "unknown"


class StepResult(BaseModel):
    step_id: str
    intent: str = ""
    action: str
    status: str = "failed"  # passed | failed | skipped（abort 后未执行的步骤）；默认 failed（fail-safe）
    duration_ms: int = 0
    used_strategy: str | None = None
    category: str | None = None  # 失败五分类；通过为 None
    #: 假红标记：分类为 env-failure 但 **App 实际在前台** ⇒
    #: 这条"环境失败"多半是传输层断连的误诊。统计时单独计，不再当环境噪声整轮作废。
    false_red: bool = False
    #: R16（2026-10-29）**这一屏还是不是用例假设的那一页**：`"left-app"`（前台已不是被测
    #: 包，如插屏广告把微信小程序拉起来）/ `"webview-page"`（App 内被换成全屏 WebView
    #: 落地页）/ `None`（看着还是被测 App 自己的页面）。
    #: ⚠️ **只打标，不改 `status` / `category`**：改类别会让历史基线不可比。判据在
    #: `off_page.py`（一处实现，判读工具也调它，所以能对旧产物回溯判定）。
    off_page: str | None = None
    # 分类来源：None/"rules"=内置规则判定；"human"=**人工校准改判过**（见 calibration.py）。
    # 报告要能标出来 —— 否则人看不出这条结论是自己判的、还是机器判的。
    category_source: str | None = None
    category_before_calibration: str | None = None  # 改判前的原始分类（留痕，便于回溯）
    retried: bool = False  # post-verify 无变化后的动作级重试
    retried_on_failure: bool = False  # on_failure=retry 的整步重试
    #: 传输层断连被**重连救回**过（P0-1）：本步内部发生过 `RemoteDisconnected` 族异常，
    #: 重建会话后重试成功。不改 status，但必须留痕——否则"这次跑批救回几次"只能翻
    #: stderr，事后无法从产物回答（实测 2026-10-25 注入：连杀 7 次 u2 服务，用例仍 100%）。
    transport_retried: bool = False
    heal: dict | None = None  # 自愈提案 {"original": ..., "match": ...}，待人工确认回写
    warning: str | None = None
    error: str | None = None
    screenshot: str | None = None  # 相对 artifacts 目录
    #: 失败步的**控件树证据**（相对 artifacts，`screens/step_XX_<id>_aN.json`）。
    #: 里面有动作前后两屏的控件摘要（text/desc/res-id/bounds）——断言写错时，正确的断言
    #: 只能从这一屏推出来，截图里没有 res-id（见 `_write_screen_evidence`）。
    screen: str | None = None
    logcat_file: str | None = None  # 相对 artifacts 目录
    # P1-1：api 步骤的证据（method/url/status/expect 明细/打码头/响应体片段/saved）。
    # UI 步骤恒为 None；证据细节见 api_client.run_api 的契约。
    api: dict | None = None
    # UI 步骤恒为 None；证据细节见 api_client.run_api 的契约。
    # 人工校准标记（发起人 2026-10-13）：只影响展示，不改 status / 成功率统计口径
    ignored: bool = False                # 失败但人工标记「可忽略」（误报/不影响产品）
    false_positive: bool = False         # 通过但人工标记「误判」（没验证到/侥幸通过）
    calibration_note: str | None = None  # 校准原因（写给后来的人）


class RunResult(BaseModel):
    case_id: str
    case_name: str = ""
    status: str  # passed | failed
    step_success_rate: float = 0.0
    duration_ms: int = 0
    failed_categories: dict[str, int] = Field(default_factory=dict)
    perf: dict = Field(default_factory=dict)  # {"launch_ms": ..., "mem_pss_mb": ...}，best-effort
    #: 用例声明的前置条件（原样带过来）。写进 run.json 是为了让"这条用例有前提"**可见** ——
    #: 否则它只活在手工用例表里，归因时会去查一个不存在的定位问题（见 preconditions.py）。
    preconditions: list[str] = Field(default_factory=list)
    #: 前置/收尾步骤的执行证据（2026-10-21）。主步骤在 `steps`；两段分开存，
    #: 避免"前置没准备好"污染步骤成功率，同时 run.json 里能看到到底准备了什么。
    setup_steps: list[StepResult] = Field(default_factory=list)
    teardown_steps: list[StepResult] = Field(default_factory=list)
    #: 执行期前置置备证据（arrival_provision）：状态不符时用图谱父链自动到达目标页。
    #: 写进 run.json 让"这条用例被自动带到过某页"**可见可审**（几跳/成败/原因），
    #: 而不是黑箱替人点了两下。needed=False 表示无事可做或做不了（reason 说明）。
    arrival: dict | None = None
    steps: list[StepResult] = Field(default_factory=list)
    operator: str = ""  # 执行人（web_api 注入触发者名字；CLI 直跑为空）——run.json/报告落盘


class Runner:
    def __init__(self, drv, artifacts_dir: Path, cred_store=None, history_path: Path | None = None,
                 vision_client=None, index_path: Path | None = None, recorder=None,
                 workspace_root: Path | None = None, quality_gate: bool = False):
        self.drv = drv
        self.artifacts = Path(artifacts_dir)
        self.recorder = recorder # 录屏器：None=不录；接口 start/stop/keep/discard
        (self.artifacts / "screenshots").mkdir(parents=True, exist_ok=True)
        self.cred_vars = cred_store.load_as_vars() if cred_store else {}
        self.history_path = (Path(history_path) if history_path
                             else _state_home() / "history.jsonl")
        self.vision_client = vision_client  # 注入则忽略 GEN_VL_MODEL 门控（单测用）
        # 页面图谱路径：显式传入优先；不传则按工作区约定 <workspace_root>/artifacts/page_index/<包名>.json
        self.index_path = Path(index_path) if index_path else None
        # 工作区根（2026-10-20 评审 P1-3）：页面图谱与校准规则的读取基准。
        # 控制台可 `--root` 指向他处、/api/workspace/switch 可在线换根，而进程 cwd 不变——
        # 再拿 Path.cwd() 当基准就会读不到控制台写入的校准规则/页面图谱（静默退化为内置规则）。
        # CLI 场景 cwd 即工作区，缺省回落 cwd 行为不变。
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self.vision_reason: str = ""  # 最近一次 vision 兜底未命中的原因（额度用尽/未命中）
        self.perf: dict = {}
        self.operator = ""  # 执行人（web_api 层注入 operator_of(body)；CLI 直跑留空）
        # 最近一次 launch 的包名：清弹窗时用来判断"App 是不是还在屏幕上"（见 app_on_screen）
        self._launched_package: str | None = None
        # 上一步是 input ⇒ 下一步 tap 前要先收软键盘（iOS：键盘顶起页面但 frame 不更新）
        self._input_since_tap = False
        # 传输层断连被重连救回的累计次数（P0-1）。步骤内做差 → 抄进 StepResult.transport_retried，
        # 产物可回答"这一跑救回几次"，不必翻 stderr。
        self._transport_retries = 0
        # **跑前闸门**（2026-10-23）：闸门只长在"生成/导入"两条路径上，**历史用例与手改用例
        # 永远绕过它** —— 实测 65 条跑测里 15 条首步不是 launch（13 条来自手工导入批次），
        # 而同一套 `review()` 对同样的用例能当场补上。
        # ⚠️ **库层默认关、产品入口显式开**（CLI `mat run` / 控制台）：`Runner` 也被校准
        # （`generate_calibrated(executor=runner.run)`）等内部路径复用，那些地方不该被
        # "跑之前偷偷改用例"影响；而测试大量直接构造 Runner 断言步序，默认开会让它们全部错位。
        self.quality_gate = quality_gate

    def _target_package(self) -> str | None:
        """当前用例的目标 App（用于"App 是否仍在屏幕上"的判定）。"""
        return self._launched_package or getattr(self.drv, "_session_bundle", None)

    def _apply_run_gate(self, case: TestCase) -> TestCase:
        """跑前过一遍**同一套**用例质量闸门（确定性修复 + issues 记进 `meta`）。

        为什么不放在 `mat run` 的命令行层：控制台、套件、MCP 都走 `Runner.run`，
        放在这里才**只有一处实现**（同 `apply_quality_gate` 的"单一闸门"纪律）。
        """
        from .ai_client import apply_quality_gate

        try:
            index_dir = self.index_path.parent if self.index_path else None
            return apply_quality_gate(case, index_dir)
        except Exception as exc:  # noqa: BLE001 —— 闸门失败不该阻断执行（同"证据缺失不改结果"）
            case.meta.setdefault("quality_issues", []).append(
                f"[X0] 跑前闸门异常，用例按原样执行: {type(exc).__name__}: {exc}")
            return case

    def run(self, case: TestCase, on_step=None, cancel_event=None) -> RunResult:
        """执行用例。on_step(StepResult) 每步定稿时回调（监控旁路，异常被吞不影响执行）；
        cancel_event 置位后在**下一步开始前**安全退出（不打断当前步，
        已完成步骤的证据照常保留，status="cancelled"）。"""
        started = time.monotonic()
        if self.quality_gate:
            case = self._apply_run_gate(case)
        variables = {**self.cred_vars, **case.vars}  # ${cred.名称} 来自凭据存储；用例 vars 同名可覆盖
        self._wake_device()
        # 防锁屏保活（iOS 真机；Android/模拟器上是 no-op）——见 BaseDriver.start_keepalive
        try:
            self.drv.start_keepalive()
        except Exception:  # noqa: BLE001 —— 保活失败不影响执行
            pass
        if self.recorder:
            # P1-2（2026-10-24）：start() 在下方主 try/finally 圈外——抛错（如 scrcpy
            # 启动即退的 RuntimeError）会绕过 finally 的 stop_keepalive()，长驻进程
            # （控制台）里保活线程泄漏到进程退出。这里先停保活再上抛：录屏起不来
            # 仍然失败可见，但环境不留悬挂资源。
            try:
                self.recorder.start()  # 全程录，失败保留/通过删除（stop 在 finally）
            except Exception:
                try:
                    self.drv.stop_keepalive()
                except Exception:  # noqa: BLE001 —— 收尾失败不掩盖原始异常
                    pass
                raise
        # **每轮清一次证据源**（T76）：崩溃证据靠系统日志，而窗口开到了整段缓冲区
        # ⇒ 上一轮留下的崩溃会让这一轮的**无关失败**被误判成 `app-crash`
        # （正是本项目最怕的假阳性）。清一次就把时效限定在本轮。
        # P2.2：改走驱动契约（Android=logcat -c，iOS=清 syslog 环形缓冲），
        # 此前直连 adb ⇒ iOS 上静默返回空，失败步没有任何系统日志证据。
        # **容错**：桩/旧驱动可能是鸭子类型、没有该方法（test_v1 的 FakeDriver 就是）——
        # 证据源清理失败不该让整条用例崩掉（同 C2 的"证据缺失不改变执行结果"纪律）。
        try:
            self.drv.start_evidence()
        except Exception:  # noqa: BLE001
            pass
        # P0-5（2026-10-19）：保活必须有始有终——控制台是长驻进程，run() 中途抛异常
        # 也要经 finally 停掉保活线程；否则任务结束后仍每 20s 按一次音量键。
        run_result: RunResult | None = None  # finally 里录屏收尾要读 status，先初始化
        try:
            results: list[StepResult] = []
            arrival_ev: dict | None = None
            arrival_done = False
            cancelled = False
            # 前置步骤（2026-10-21）：先置备数据/状态，失败 ⇒ 整条用例 skipped（不是 failed）。
            # 理由同 preconditions 拒跑：把"数据没准备"报成定位/断言失败，会把排查带偏。
            setup_results, setup_fail = self._run_phase(
                case.setup, variables, case, on_step, phase="setup")
            if setup_fail is not None:
                for rest in case.steps:
                    skipped = StepResult(
                        step_id=rest.id, intent=rest.intent, action=rest.action.value,
                        status="skipped",
                        error=f"前置步骤 {setup_fail} 失败（用例未执行）",
                    )
                    results.append(skipped)
                    if on_step is not None:
                        try:
                            on_step(skipped)
                        except Exception:
                            pass
            for idx, step in enumerate(case.steps if setup_fail is None else [], 1):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                # 执行期前置置备（「求解器」最小实现）：只在**首个非 launch 步骤**前
                # 尝试一次——launch 后设备在首页，A8 类用例却去点深层页面的控件 ⇒
                # 先按图谱父链自动到达（推断不出/已在该页/失败都静默降级为原行为）。
                if not arrival_done and step.action != Action.LAUNCH:
                    arrival_done = True
                    arrival_ev = self._ensure_arrival(case)
                result = self._attempt(idx, step, case, variables)
                # 可选步骤（onFailure=continue，2026-10-13）：定位失败 ⇒ 目标没出现（典型：
                # 启动广告已消失/弹窗没弹）——记 skipped 继续，不算失败不污染通过率；
                # 其他异常保持 failed（真实问题要露头），但同样不触发 abort。
                if (result.status == "failed" and step.on_failure == OnFailure.CONTINUE
                        and "LocateFailed" in (result.error or "")):
                    result.status = "skipped"
                    result.error = f"可选步骤（onFailure=continue）：{result.error}；目标未出现，已继续"
                results.append(result)
                if on_step is not None:
                    try:
                        on_step(result)
                    except Exception:
                        pass
                if result.status == "failed" and step.on_failure == OnFailure.ABORT:
                    # onFailure=abort（DSL 默认）：失败即停——后续步骤在本轮无意义，如实记 skipped。
                    # 此前 abort 从未被读取（T43，A5 族"配置要真的生效"）：步骤失败后永远继续，
                    # 白烧真机预算、失败集被级联噪声放大（评测线实测 assert-fail 后照样跑完 10 步）。
                    # heal / retry 步骤失败不触发 abort（各自策略语义不变）。
                    for rest in case.steps[idx:]:
                        skipped = StepResult(
                            step_id=rest.id, intent=rest.intent, action=rest.action.value,
                            status="skipped",
                            error=f"前序步骤 {step.id} 失败（onFailure=abort），本步未执行",
                        )
                        results.append(skipped)
                        if on_step is not None:
                            try:
                                on_step(skipped)
                            except Exception:
                                pass
                    break

            # 收尾步骤（2026-10-21）：best-effort，失败只记录、不改状态（收尾不该判定用例成败）
            teardown_results, _ = self._run_phase(
                case.teardown, variables, case, on_step, phase="teardown")

            failed = [r for r in results if r.status == "failed"]
            categories: dict[str, int] = {}
            for r in failed:
                categories[r.category or "unknown"] = categories.get(r.category or "unknown", 0) + 1
            rate = (sum(r.status == "passed" for r in results) / len(results)) if results else 0.0

            serial = getattr(self.drv, "serial", None)
            package = case.target.get("package", "")
            if serial and package:
                mem = mem_pss_mb(serial, package)
                if mem is not None:
                    self.perf["mem_pss_mb"] = mem

            run_result = RunResult(
                case_id=case.case_id,
                case_name=case.name,
                status=("cancelled" if cancelled else
                        ("skipped" if setup_fail is not None
                         else ("failed" if failed else "passed"))),
                step_success_rate=rate,
                duration_ms=int((time.monotonic() - started) * 1000),
                failed_categories=categories,
                perf=self.perf,
                preconditions=list(case.preconditions),
                setup_steps=setup_results,
                teardown_steps=teardown_results,
                arrival=arrival_ev,
                operator=self.operator,
                steps=results,
            )
            self._persist(case, run_result)
            return run_result
        finally:
            try:
                self.drv.stop_keepalive()
            except Exception:  # noqa: BLE001 —— 收尾失败不影响已产出的结果
                pass
            # 录屏收尾也必须在 finally（2026-10-20 评审 P2-3）：此前只在成功路径 stop，
            # _persist 写盘抛错等异常路径会泄漏 scrcpy/screenrecord 子进程，
            # 控制台是长驻进程，泄漏会累积。stop 幂等（已停再停返回 []）。
            if self.recorder:
                try:
                    files = self.recorder.stop()
                    if files:
                        # 通过 ⇒ 删（磁盘纪律）；失败/中止/异常 ⇒ 挪进 artifacts/video/（报告相对引用）
                        status = run_result.status if run_result is not None else "failed"
                        (self.recorder.discard(files) if status == "passed"
                         else self.recorder.keep(files, self.artifacts))
                except Exception:  # noqa: BLE001 —— 录屏收尾失败不掩盖执行结果
                    pass

    def run_datasets(self, case: TestCase, root: Path | None = None,
                     on_step=None, cancel_event=None,
                     recorder_factory=None) -> list[tuple[str, RunResult, Path]]:
        """数据驱动（CASE-06）：每个数据组独立执行一轮，返回 [(组名, 结果, 产物目录)]。

        变量合并优先级：数据组 vars > 用例 vars > 凭据；各组产物独立目录。
        on_step / cancel_event 透传给每轮 run（监控与中止对数据驱动同样生效）。

        recorder_factory（P1-5，2026-10-24）：`Callable[[组产物目录], 录屏器|None]`——
        数据驱动逐组挂录屏。此前 `--record` 在此路径被**静默丢弃**（单条/套件都有
        录屏，数据驱动连探测都不做、无任何警告）。Recorder 是一次性对象
        （staging 目录 + `_started` 状态），每组各建一个、落在本组产物目录下。
        """
        datasets = case.datasets or [{"name": "默认"}]
        results = []
        base_root = Path(root) if root else self.artifacts
        saved_recorder = self.recorder
        try:
            for n, ds in enumerate(datasets):
                name = str(ds.get("name") or ds.get("vars", {}).get("name") or f"组{n + 1}")
                ds_vars = dict(ds.get("vars") or {})
                merged_case = case.model_copy(deep=True)
                merged_case.datasets = None  # 防递归
                merged_case.vars = {**case.vars, **ds_vars}
                # 旧净化不处理点号，`name=".."` 会让产物写到
                # base_root 上一级（root/".."+"/run.json"）。点段与首尾点号一并拦掉，
                # 并加 resolve 后的越界断言兜底——净化漏网的任何形状都不许写出 base_root。
                sanitized = re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")
                if sanitized in ("", ".", "..") or sanitized.startswith("."):
                    sanitized = f"组{n + 1}"
                artifacts = base_root / sanitized
                if not artifacts.resolve().is_relative_to(Path(base_root).resolve()):
                    artifacts = base_root / f"组{n + 1}"
                self.artifacts = artifacts
                self.recorder = recorder_factory(artifacts) if recorder_factory else None
                (artifacts / "screenshots").mkdir(parents=True, exist_ok=True)
                results.append((name, self.run(merged_case, on_step=on_step,
                                               cancel_event=cancel_event), artifacts))
            return results
        finally:
            self.recorder = saved_recorder

    def _ensure_arrival(self, case: TestCase) -> dict:
        """执行期前置置备（「求解器」最小实现）。

        **永不抛出**：任何失败都降级为「按原样继续跑」——置备是增益，不是前置依赖。
        证据（几跳/成败/原因）原样返回，由 RunResult.arrival 落盘可见。
        """
        from .arrival_provision import auto_arrive_enabled, provision

        if not auto_arrive_enabled():
            return {"needed": False, "ok": None, "reason": "已关闭（MAT_AUTO_ARRIVE=0）"}
        package = case.target.get("package", "")
        if not package:
            return {"needed": False, "ok": None, "reason": "用例无 target.package"}
        # 图谱路径与 __init__ 同约定：显式传入优先，否则 <workspace_root>/artifacts/page_index/<包名>.json
        index_path = self.index_path or (self.workspace_root / "artifacts" / "page_index"
                                         / f"{package}.json")
        if not Path(index_path).exists():
            return {"needed": False, "ok": None, "reason": f"无页面图谱: {index_path}"}
        ev = provision(case, self.drv, package, index_path)
        if ev.get("ok"):
            print(f"前置置备: 已自动到达「{ev.get('page', '')}…」"
                  f"（{ev.get('hops', 0)} 跳 / {ev.get('clicks', 0)} 次点击）")
        elif ev.get("needed"):
            print(f"前置置备未成功（{ev.get('reason', '')}）——用例按原样继续")
        return ev

    def _wake_device(self) -> None:
        """执行前 best-effort 唤醒/解锁——锁屏是执行失败与调用挂死的头号环境因素。

        C1（2026-10-12 评审）：唤醒是 BaseDriver 契约方法 `wake_and_unlock()`。
        旧实现判据是 `getattr(drv, "raw", None) is None ⇒ 桩`——但 `raw` 随 P0 迁移
        从真实驱动上删除后，该判据对真机同样恒成立 ⇒ 唤醒防护实际从未生效。
        """
        try:
            self.drv.wake_and_unlock()
        except Exception:
            pass  # 桩未实现契约（单测）/真机唤醒失败，都不阻塞执行

    def _persist(self, case: TestCase, run_result: RunResult) -> None:
        """run.json（配置台/自愈回写/趋势的数据源）+ 本地执行历史（透明性设计：产物可自行打开核对）。"""
        # **被测 App 版本**（T76 口径漏洞）：对外数字必须能回答"测的是哪个构建"。
        # `explore` 的图谱一直记它，`runner` 一直没记 ⇒ 靶场源码改过之后
        # （T64 +4 缺陷、T76 +1 崩溃），**查不出旧数字是在哪个版本上测的**
        # （那条"在当前 revision 上可复现"只追踪了引擎，漏了靶场）。
        from .driver import app_apk_sha256, app_version

        target_pkg = (case.target or {}).get("package") or ""
        run_payload = run_result.model_dump()
        # 批次标识：套件/数据驱动批量运行时由调用方
        # 设置 Runner.batch（id/suite/index/total），报告页据此把同批条目归组折叠；
        # 单条运行为 None，run.json 不带该字段（旧数据天然兼容）。
        batch = getattr(self, "batch", None)
        if batch:
            run_payload["batch"] = batch
        payload = {
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "device_serial": getattr(self.drv, "serial", None),
            # 平台 + 设备身份（P2.2）：对外数字必须能回答"测的是哪台、哪个系统"。
            # Android 从 getprop 读，iOS 从 WDA device_info/status 读；取不到就是空串。
            "platform": getattr(self.drv, "platform", "android"),
            "device": self._device_meta(),
            "app_version": app_version(self.drv, target_pkg),
            # **安装包指纹**：所有靶场变体共用 `versionName=1.0` ⇒ 光看版本号
            # 分辨不出 base / v09-defect-free，指纹才能回答"测的是哪个构建"。
            "app_apk_sha256": app_apk_sha256(self.drv, target_pkg),
            "case": json.loads(dump_dsl(case)),
            "run": run_payload,
        }
        # P1-1（2026-10-24 评审）：run.json 写不进去不该反过来毁掉一次**已完成的真机
        # 执行——此前磁盘满/权限错会让整个 run() 抛异常，调用方连 RunResult 都拿不到
        # （报告/聚合/自愈回写全部丢失）。同文件 history/证据落盘的既有纪律：
        # 产物缺失降级为**可见告警**，不改执行结果。
        try:
            (self.artifacts / "run.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"⚠️ run.json 写盘失败（{exc}）：产物目录 {self.artifacts} 可能已满或无权限；"
                  "步骤结果仍会返回，但报告/自愈回写/趋势将读不到本次执行",
                  file=sys.stderr)
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": payload["finished_at"],
                "case_id": run_result.case_id,
                "case_name": run_result.case_name,
                "status": run_result.status,
                "step_success_rate": run_result.step_success_rate,
                "duration_ms": run_result.duration_ms,
                "failed_categories": run_result.failed_categories,
            }
            with self.history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 历史记录失败不掩盖执行结果

    # -- 内部 ---------------------------------------------------------------

    def _write_screen_evidence(self, idx: int, step, result: StepResult, before_xml: str,
                               after_xml: str, digest_after: dict, pkg: str, attempt: int,
                               foreground: str | None = None) -> None:
        """把失败步**动作前后**的控件摘要落盘（`screens/step_XX_<id>_aN.json`）。

        ## 为什么必须落盘（2026-10-24）

        断言写错（A2 断言与导航耦合 / A3 内容快照）时，**正确的断言只能从真机那一屏推出来**。
        此前 `run.json` 只留了截图：截图里没有 `res-id`，人得肉眼抄文案，机器更没法用；
        图谱也补不上这个洞 —— 内容型 App 的入口（「早晚报」「播客」「新闻妹」）常常压根
        没被探索到（实测 tx-news 44 页图谱里没有早晚报页，于是 A2 的"到达断言"自动改写
        覆盖率只有 1/10——图谱没探索到入口页时改写帮不上）。

        ## 为什么是"前后两屏"而不是只留当前屏

        判"哪个控件能证明到了目标页"= **动作后新出现的、且稳定的**那一个。只有一屏时
        无法区分"本来就在（tab/底栏）"和"刚出现（目标页的独有控件）"——
        而这正是 R2（必失败）与 R23（恒真）的分界。

        ## 纪律

        - **失败才写**：通过的步骤写它只是噪音，而且每步多一次 30-50KB 落盘；
        - **写失败不影响执行**：证据缺失不该改判定（与"日志缺失不改结果"同口径），
          所以整体包在调用方的 `try/except` 里，这里再单独兜一次 IO 异常。
        """
        from .page_index import digest_page, page_key
        from .ui_tree import parse_nodes

        try:
            before_digest = digest_page(before_xml, pkg) if before_xml else {"controls": []}
            # 前台包名与"这一屏到底有谁的控件"必须一起留：实测（2026-10-24）「早晚报」那一跳
            # 动作后**被测 App 的控件一个都没有**（`after` 空），只留控件列表的话，
            # 下游只能猜"整屏都是内容标题"——而真相是这一屏不属于被测 App。
            after_nodes = parse_nodes(after_xml) if after_xml else []
            payload = {
                "step_id": step.id,
                "intent": step.intent,
                "action": step.action.value,
                "target": (step.target.model_dump() if step.target is not None else None),
                "error": result.error,
                "package": pkg,
                # 页签名：判断"这一屏到底有没有变"，比人眼看文案可靠（tab 切换常常只变内容）
                "before_page": page_key(before_xml, pkg) if before_xml else None,
                "after_page": page_key(after_xml, pkg) if after_xml else None,
                "before": before_digest.get("controls") or [],
                "after": digest_after.get("controls") or [],
                # 诊断用：节点总数 / 出现过的包 / 前台是谁（"0 个控件"要能解释，不许让人猜）
                "after_nodes": len(after_nodes),
                "after_packages": sorted({n.package for n in after_nodes if n.package})[:5],
                # 调用方已经取过一次就复用（少一次 adb 往返，也避免两处取值不一致：
                # 同一屏的"前台是谁"与"是否已换页"必须是同一个答案）
                "foreground": foreground if foreground is not None else self._foreground_package(),
            }
            path = self.artifacts / "screens" / f"step_{idx:02d}_{step.id}_a{attempt}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            result.screen = str(path.relative_to(self.artifacts))
        except OSError:
            pass  # 证据写不进去不该改变执行结果

    def _foreground_package(self) -> str:
        """前台包名（best-effort）：`dump` 里一个被测 App 的控件都没有时，这是唯一的解释线索。"""
        try:
            return str((self.drv.foreground_app() or {}).get("package") or "")
        except Exception:      # noqa: BLE001 - 诊断信息，拿不到就空着
            return ""

    def _rid_hints(self, case: TestCase, res_id: str) -> list[str]:
        """取某个 res-id 的历史文案线索（图谱里查）。图谱不存在/读不了就返回空，不报错。"""
        from .page_index import rid_hints

        path = self.index_path
        if path is None:
            package = (case.target or {}).get("package", "")
            if not package:
                return []
            # A3 同口径：包名形状非法（路径逃逸尝试）时线索本就是 best-effort，直接放弃。
            from .pkgname import is_safe_package
            if not is_safe_package(package):
                return []
            path = self.workspace_root / "artifacts" / "page_index" / f"{package}.json"
        return rid_hints(path, res_id)

    @staticmethod
    def _vision_allowed(step) -> bool:
        """是否允许调用**计费**的视觉兜底。

        默认**不允许**——计费能力不该在用户不知情时触发。显式开启两种方式：
          · 步骤标 `onFailure: heal`（逐步骤授权）
          · 环境变量 `GEN_VL_FALLBACK=1`（全局授权）
        注意：本地模糊自愈（零成本）不受此门控，它总是先跑。
        """
        return step.on_failure == OnFailure.HEAL or os.environ.get("GEN_VL_FALLBACK") == "1"

    def _run_phase(self, steps, variables: dict[str, str], case: TestCase,
                   on_step, *, phase: str) -> tuple[list[StepResult], str | None]:
        """执行 setup / teardown 段（2026-10-21）。

        返回 (步骤结果, 失败步骤 id)。`phase="setup"` 时首个失败即停并返回该步 id
        （调用方据此把整条用例记 skipped）；`teardown` 是 best-effort——失败继续跑完，
        但返回值恒为 None（不影响用例状态）。

        步骤序号用 1000+ 偏移：截图/证据文件名不与主步骤（1..n）重名。
        """
        results: list[StepResult] = []
        for i, step in enumerate(steps or [], 1):
            result = self._attempt(1000 + i, step, case, variables)
            if (result.status == "failed" and step.on_failure == OnFailure.CONTINUE
                    and "LocateFailed" in (result.error or "")):
                result.status = "skipped"
                result.error = f"可选步骤（onFailure=continue）：{result.error}；目标未出现，已继续"
            results.append(result)
            if on_step is not None:
                try:
                    on_step(result)
                except Exception:
                    pass
            if result.status == "failed":
                if phase == "setup":
                    return results, step.id
        return results, None

    def _attempt(self, idx: int, step, case: TestCase, variables: dict[str, str]) -> StepResult:
        """按 on_failure 策略执行：retry 整步重试一次（合并为一个结果并标注）。"""
        result = self._run_step_once(idx, step, case, variables)
        if result.status == "failed" and step.on_failure == OnFailure.RETRY:
            retry_result = self._run_step_once(idx, step, case, variables, attempt=2)
            if retry_result.status == "passed":
                retry_result.retried_on_failure = True
                retry_result.warning = f"首跑失败已重试通过（首跑: {result.error}）"
                return retry_result
        return result

    def _with_transport_retry(self, op):
        """假红治理：传输层断连 ⇒ 重建驱动会话**重试一次**。

        只包**幂等的前置动作**（首帧 dump / 定位 / app_start）——动作执行后的
        阶段不许重试（重试一次 tap 就是双击）。驱动没有 reconnect（iOS/桩）时
        原样上抛，行为不变。
        """
        try:
            return op()
        except Exception as exc:
            from .driver import is_transport_error

            if not is_transport_error(exc):
                raise
            reconnect = getattr(self.drv, "reconnect", None)
            if not callable(reconnect):
                raise
            print(f"⚠️ 传输断连（{type(exc).__name__}），重建会话重试一次", file=sys.stderr)
            try:
                reconnect()
            except Exception:  # noqa: BLE001 —— 重连失败则让原始异常走原路径
                raise exc from None
            # 记到实例上，由 `_run_step_once` 抄进 StepResult（产物可审：救回过几次）
            self._transport_retries += 1
            return op()

    def _run_step_once(self, idx: int, step, case: TestCase, variables: dict[str, str],
                       attempt: int = 1) -> StepResult:
        # P1-1：api 步骤**不需要设备**——提前返回，跳过 wait_stable/定位/断言/截图/分类链。
        # 失败分类直接置 "api"；证据进 result.api（run.json/报告持久化）。
        if step.action == Action.API:
            return self._run_api_step(step, variables)
        t0 = time.monotonic()
        result = StepResult(step_id=step.id, intent=step.intent, action=step.action.value)
        retries_before = self._transport_retries  # 本步内是否发生过"断连→重连→救回"
        # 文件名带 attempt 后缀——retry 二次执行此前会覆盖
        # 首跑同名截图，"每步留证据"丢的正是"第一次为什么失败"的那张图。
        shot = self.artifacts / "screenshots" / f"step_{idx:02d}_{step.id}_a{attempt}.png"
        logcat_path = self.artifacts / "logs" / f"step_{idx:02d}_{step.id}_a{attempt}.logcat.txt"
        # 失败时的"动作前"控件树（证据落盘要用）。在 try 外初始化：`wait_stable_xml`
        # 自己也可能抛，那时它没有值 —— 若在 except 里才赋值就会 NameError 把证据也吞掉。
        xml_before = ""

        try:
            # iOS：系统弹窗可能在任何时刻盖住前台，每步开头清一次（Android 上无操作）
            if getattr(self.drv, "platform", "") == "ios":
                self._dismiss_system_alerts()
            stable, xml_before = self._with_transport_retry(
                lambda: wait_stable_xml(self.drv))

            element: Element | None = None
            # **launch 步骤的 target 不是"要点哪个控件"**，而是"启动后应出现的锚点"
            # （LAUNCH 分支根本不使用 element）。启动前前台还是上一个 App / 桌面，
            # 此时解析它必然失败——2026-10-18 真机实测：3 条用例在 s1 就红，
            # 模拟器上因为 App 常已在前台而侥幸通过（掩盖了这个 bug）。
            if step.target is not None and step.action != Action.LAUNCH:
                element = self._with_transport_retry(
                    lambda: resolve(self.drv, self._subst(step.target, variables),
                                    step.timeout_ms))
                result.used_strategy = element.strategy

            before = hashlib.md5(xml_before.encode()).hexdigest()  # 复用空闲判定的最后一次 dump
            self._act(step, case, element, variables)
            _pause(0.3)
            # 动作自身带回的可复核结论（如「清空后回读仍为 X」）。**必须落盘**：
            # `Element.clear_text` 采到了却没人拼进 warning，等于没采（与 C17 同类）。
            # 单独成一段而不是塞进下面「控件树无变化」分支：清空失败时树**会**变
            # （键盘/联想词变了），走不到那个分支。
            if element is not None and getattr(element, "note", None):
                result.warning = (f"{result.warning}；{element.note}"
                                  if result.warning else element.note)

            # post-verify（外部评审 P1-3）：动作后的稳定树由 wait_stable_xml 一次性给出，
            # 两处 _tree_hash() 的独立 dump 已消除——交互步 dump 6→4，非交互步保持 2
            if step.action in _INTERACTIVE:
                _, xml_after = wait_stable_xml(self.drv)
                if hashlib.md5(xml_after.encode()).hexdigest() == before:
                    if step.action in _REPLAYABLE:
                        # 无变化可能是点到弹窗/未生效 → 重试一次（02 §3.2 第 4 条）
                        self._act(step, case, element, variables)
                        _pause(0.3)
                        _, xml_after = wait_stable_xml(self.drv)
                        if hashlib.md5(xml_after.encode()).hexdigest() != before:
                            result.retried = True
                        else:
                            # ⚠️ 文案要**点出软键盘**：它是"树无变化"最常见的真因，而且极难反推——
                            # 实测（2026-10-05，T63）：控件被软键盘盖住时 `input tap` 落在输入法窗口上，
                            # 被测 App 根本收不到事件；用户看到的是**后面几步莫名定位失败**，
                            # 真因埋在这一步。**自动收起键盘做不到可靠探测**（本机当前 IME 是
                            # uiautomator2 的 AdbKeyboard，它没有 UI ⇒ `mInputShown` 恒为 false，
                            # 而真实键盘确实在屏幕上），所以这里只把可能的原因说清、不自动动作。
                            result.warning = ("动作后控件树无变化（可能为无副作用操作、被弹窗拦截，"
                                              "或**控件被软键盘遮挡**——可先确认该控件是否在键盘区域内）")
                    else:
                        # 输入类不重放（文本重复输入/覆盖的风险大于重放收益），
                        # 只提示可能未生效，后续断言自会暴露脏数据。
                        _no_replay = ("输入类动作后控件树无变化——**不自动重放**（避免重复输入/覆盖字段）；"
                                      "若文本未生效请检查输入框焦点与软键盘状态")
                        # 追加而不是覆盖：上面可能已有「清空后回读仍为 X」这类结论，
                        # 覆盖掉就等于把更具体的那条证据丢了。
                        result.warning = (f"{result.warning}；{_no_replay}"
                                          if result.warning else _no_replay)

            self._check_assertions(step, variables, step.timeout_ms)
            result.status = "passed"
        except Exception as exc:
            raw_error = f"{type(exc).__name__}: {exc}"
            result.status = "failed"
            result.error = raw_error
            try:  # 失败归因增强：附当前页可见控件摘要（定位失败时人工复核的关键上下文）
                from .page_index import digest_page
                # ⚠️ **必须传被测包名**——`digest_page` 靠它过滤掉系统 UI/输入法。
                # 不传（`package=""`）时过滤失效，而**系统 UI 节点在 a11y 树里更靠前**，
                # 于是这 8 条全被状态栏/导航栏占满、**App 自己的控件一个都进不来**。
                # 实测后果（2026-10-03，T28）：LLM 归因看到"可见控件=返回/主屏幕/切换输入法"
                # 就判"页面是系统界面、前置没生效"，把**真产品缺陷**误判成 `precondition`。
                pkg = (case.target or {}).get("package", "")
                after_xml = self.drv.dump()          # 一次 dump 供"可见控件提示"与证据复用
                digest_after = digest_page(after_xml, pkg)
                # R16（2026-10-29）：先判"这一屏还是不是用例假设的那一页"，再落证据——
                # 它是**纯函数**（不抛异常），放在证据写入之前，免得证据 IO 失败把它一起吞掉。
                # 为什么要这条：插屏广告把微信小程序拉起来时，`text=我的` 的报错和
                # "定位值写错"一模一样，首跑通过率判读会把它算成用例问题（详见 `off_page.py`）。
                foreground = self._foreground_package()
                result.off_page = off_page_kind(pkg, digest_after.get("controls") or [], foreground)
                # 控件树证据落盘（2026-10-24）：断言写错（A2/A3）时，**正确的断言只能从
                # 真机那一屏推出来**，而 run.json 此前只留截图 —— 截图里没有 res-id，
                # 人得肉眼抄文案，机器更没法用；图谱也补不上（内容型 App 的入口常常没被探索到）。
                # ⚠️ 顺序（2026-10-26）：**先落盘再拼提示**——提示里要引用 `result.screen`，
                # 否则读提示的人会以为"可见控件只有这 8 个"（实测就是这样误判的）。
                self._write_screen_evidence(idx, step, result, xml_before, after_xml,
                                            digest_after, pkg, attempt, foreground)
                all_labels = [str(c.get("text") or c.get("desc") or "")[:20]
                              for c in digest_after["controls"]]
                all_labels = [x for x in all_labels if x]
                labels = all_labels[:8]
                if labels:
                    # 归因提示**追加不覆盖**——此前无条件赋值会
                    # 挤掉同字段里更有价值的 post-verify 告警（树无变化/键盘遮挡）。
                    shown = f"当前页面可见控件: {'、'.join(labels)}"
                    if len(all_labels) > len(labels):
                        # **别让人把"前 8 个"当成"全部"**：判「定位值是否存在」必须用完整清单，
                        # 而只给 8 条时读者只能得出"目标值不在候选集"的错误结论
                        # （一次归因复盘就是拿这 8 条判的，后用一个实验推翻了那个结论）。
                        shown += (f"（共 {len(all_labels)} 个，仅列前 {len(labels)}；"
                                  f"完整清单见 {result.screen}）" if result.screen
                                  else f"（共 {len(all_labels)} 个，仅列前 {len(labels)}）")
                    result.warning = f"{result.warning}；{shown}" if result.warning else shown
            except Exception:
                pass

            # 自愈与兜底（；仅定位类失败且带 target 时触发）。
            #
            # **两级成本的门控是刻意反过来的**（2026-09-30 修正）：
            #   ① 本地模糊重定位：零成本 → **默认跑**。公开文档承诺自愈链是自动行为，
            #      而这一步不花钱，没有理由要求用户先知道某个 flag。
            #   ② vision-llm 视觉兜底：按次计费 → **必须显式开启**（见 `_vision_allowed`）。
            #      此前它不受 on_failure 门控，只要配了 VL 模型就自动跑——于是变成
            #      「免费的不跑、收费的自动跑」，用户会在不知情的情况下被计费。
            if isinstance(exc, LocateFailed) and step.target is not None:
                healed, via = None, None
                # ① 图谱自愈：res-id 改名时，用图谱里该 id 的**历史文案**重定位（零成本）。
                #    这条专治最常见的改版形式，而通用本地模糊治不了它——后者的 token 是
                #    id 片段（`login_btn`），匹配不到界面文案（"登录"）。详见 page_index.rid_hints。
                if step.target.strategy == LocatorStrategy.RES_ID:
                    hints = self._rid_hints(case, step.target.value)
                    if hints:
                        healed = heal_by_hints(self.drv, hints)
                        via = "index-hints"
                # ② 通用本地模糊：从原定位器推导 token 模糊匹配 text/desc。零成本，默认跑。
                if healed is None:
                    try:
                        healed = heal_fuzzy(self.drv, self._subst(step.target, variables))
                        via = "local-fuzzy"
                    except Exception:
                        healed = None
                if healed is None and self._vision_allowed(step):
                    healed = self._vision_locate(step, variables)
                    via = "vision-llm"
                if healed is not None:
                    try:
                        result.heal = {
                            "original": f"{step.target.strategy.value}={step.target.value}",
                            "match": f"{healed.strategy}={healed.value}",
                            "via": via,
                        }
                        before_heal = self._tree_hash()
                        # 视觉兜底是**按坐标点击**，落点可能在 canvas / WebView / 自绘控件上
                        # ——那里内容变了也不进控件树。所以这一类额外留一张画面指纹，
                        # 只在树哈希判定"没变"时才拿来做第二判据（纯树内自愈不需要）。
                        before_frame = (_frame_signature(self.drv) if via == "vision-llm"
                                        else None)
                        self._act(step, case, healed, variables)
                        _pause(0.3)
                        after_heal = self._tree_hash()  # 自愈动作同样做 post-verify
                        if after_heal == before_heal:
                            # 界面没动：再给一次观察窗口复测。0.3s 对慢界面偏短，
                            # 立刻判死会把"只是没来得及变"误判成"点空了"。
                            _pause(SELF_HEAL_SETTLE_EXTRA_SECONDS)
                            after_heal = self._tree_hash()
                        self._check_assertions(step, variables)
                        result.used_strategy = healed.strategy
                        # 树没变 ⇒ 再用画面指纹区分「自绘界面点中了」与「真点空了」。
                        frame_state: bool | None = None
                        if after_heal == before_heal and via == "vision-llm":
                            frame_state = _frame_changed(before_frame,
                                                         _frame_signature(self.drv))
                        if after_heal == before_heal and frame_state is not True:
                            # 2026-10-27：点了坐标、界面纹丝不动 ⇒ **不算自愈成功**。
                            # 此前这里只追加一句 warning、status 照样置 passed，于是生产
                            # 数据里 35 次「vision-llm 自愈成功」有 7 次（20%）是这种假成功
                            # ——报告说"已兜底命中"，人以为好了，实际那一步什么都没发生。
                            # 提案（result.heal）仍然保留：它是有价值的候选，只是没被采纳。
                            reason = "画面指纹一致" if frame_state is False else "画面指纹不可用"
                            result.status = "failed"
                            result.error = (
                                f"{raw_error} | {via} 兜底点击 {healed.value} 后界面无任何变化"
                                f"（{0.3 + SELF_HEAL_SETTLE_EXTRA_SECONDS:.1f}s 内控件树哈希未变、"
                                f"{reason}，坐标未命中可交互元素，不计为自愈成功）"
                            )
                            result.warning = f"{via} 提案已保留，待人工确认回写"
                        else:
                            result.status = "passed"
                            result.error = None
                            if after_heal == before_heal:
                                # 自绘界面命中：树没动但画面动过，必须写清依据，
                                # 否则事后看报告会以为是旧版那种"无变化也记成功"。
                                result.warning = (
                                    f"定位失败已由 {via} 兜底命中（控件树无变化、画面已改变："
                                    f"自绘/WebView 界面），提案待人工确认回写"
                                )
                            else:
                                result.warning = f"定位失败已由 {via} 兜底命中，提案待人工确认回写"
                    except Exception as heal_exc:
                        result.error = f"{raw_error} | {via} 兜底后执行失败: {type(heal_exc).__name__}: {heal_exc}"
                else:
                    result.error = f"{raw_error} | vision-llm {self.vision_reason}"
                    # 用例声明了前置条件 ⇒ 把"控件定位不到"与"前提根本不成立"区分开，
                    # 否则人会去查一个不存在的定位问题（实测：某资讯类客户端 7 条假定位失败）。
                    # ⚠️ 措辞里**避开**「定位失败」「断言失败」两个词 —— 它们是
                    # `_classify_builtin` 的分类关键词，写进去会干扰判定。
                    if case.preconditions:
                        result.error += (
                            "｜⚠️ 本用例声明的前置条件：" + "、".join(case.preconditions)
                            + "（该步失败可能源于**前置条件未满足**，而不是控件不存在）")
        finally:
            if result.status == "failed":
                # logcat 仅失败步骤采集（通过步骤不付 adb 往返成本）。
                # C2（2026-10-12 评审）：serial 未解析时裸 `adb logcat` 在多设备下直接报错
                # ⇒ 证据静默丢失，而 app-crash 的唯一硬证据就是 logcat 的 FATAL EXCEPTION。
                # 把"没有证据"本身暴露成 warning，而不是伪装成"正常无日志"。
                if getattr(self.drv, "serial", None) is None:
                    missing = "logcat 证据缺失（设备 serial 未解析，多设备下无法定向抓取）——真崩溃可能被误分类"
                    result.warning = f"{result.warning}；{missing}" if result.warning else missing
                    logcat_text = ""
                else:
                    logcat_text = self._capture_logcat()
                    if not logcat_text:
                        # 有 serial 但**没有证据源**：必须显式告警，否则"没有证据"会被读成
                        # "没有日志"——这正是 P2.2 要堵的洞。原因按平台分开说（2026-10-25
                        # 真机评测：本机无 adb CLI 时旧文案误报成"iOS 未装 pymobiledevice3"）。
                        if getattr(self.drv, "platform", "") == "android":
                            cause = "adb 不可用（CLI 未安装或设备通道异常）"
                        else:
                            cause = "该平台没有可用证据源（如 iOS 未装 pymobiledevice3）"
                        missing = (f"系统日志证据缺失（{cause}）"
                                   "——真崩溃可能被误分类")
                        result.warning = (f"{result.warning}；{missing}"
                                          if result.warning else missing)
                # 人工校准规则叠在内置判定之上（`calibration/rules.json`）——
                # 「校准一次，下次不再犯」就落在这里。规则表读不动时 `load_rules` 返回空表，
                # 退化成纯内置判定（**不许因为规则坏了而影响执行**）。
                from .calibration import load_rules
                result.category = classify_failure(
                    result.error or "", logcat_text, load_rules(self.workspace_root))
                if result.category == "env-failure":
                    # P0-1 假红复核：env 判定的隐含前提是「App 掉出前台/无响应」；
                    # App 其实稳在前台 ⇒ 这条 env 多半是传输层假红，标记出来
                    # （统计口径：false_red 不计入环境噪声，单独看）。
                    try:
                        fg = (self.drv.foreground_app() or {}).get("package") or ""
                        pkg = ((case.target or {}).get("package")
                               or getattr(self, "_launched_package", "") or "")
                        if fg and pkg and fg == pkg:
                            result.false_red = True
                            note = "false_red：判为环境失败但 App 在前台（疑似传输层假红）"
                            result.warning = f"{result.warning}；{note}" if result.warning else note
                    except Exception:  # noqa: BLE001 —— 复核失败不影响原分类
                        pass
                if logcat_text:
                    logcat_path.parent.mkdir(parents=True, exist_ok=True)
                    logcat_path.write_text(logcat_text, encoding="utf-8")
                    result.logcat_file = str(logcat_path.relative_to(self.artifacts))
            try:
                self.drv.screenshot(str(shot))
                result.screenshot = str(shot.relative_to(self.artifacts))
            except Exception:
                pass  # 设备离线时截图失败不掩盖原始错误
            result.duration_ms = int((time.monotonic() - t0) * 1000)
            # 重连救回留痕（P0-1）：与 false_red 互补——false_red 是"没救回来"，
            # 这里是"救回来了"。两者都在 run.json 里，事后能算出这次跑批的噪声量。
            if self._transport_retries > retries_before:
                result.transport_retried = True
                note = "传输断连已重连救回（本步内重建过一次 u2 会话）"
                result.warning = f"{result.warning}；{note}" if result.warning else note
        return result

    def _vision_locate(self, step, variables) -> Element | None:
        """vision-llm 兜底定位：截图+意图 → VL 模型 → 像素坐标（置信度红线 0.7）。

        **官方代理**按档位月上限核销，额度耗尽时优雅降级（等同未启用），步骤退回定位失败
        而不中断本地执行，原因写入 self.vision_reason；**自带 Key 只计数不限额**
        （token 由用户自己的服务商结算，见 `quota.check_vision(enforce=False)`）。
        """
        from . import vision
        from .locator import Element as _Element
        from .quota import QuotaExceeded, UsageStore

        self.vision_reason = "未启用（检查 GEN_VL_MODEL / GEN_API_KEY）"
        if not (self.vision_client or vision.enabled()):
            return None
        if step.target is None:
            return None

        # 注入客户端（单测）不计配额，保持测试无副作用
        before_call = None
        if self.vision_client is None:
            from . import promptstore
            from .offline import is_loopback

            # 三种情形，成本归属各不相同：
            #   · 官方代理（平台出 token）：按档位限额，enforce=True；
            #   · 自带 Key（用户出 token）：只计数不拦，enforce=False；
            #   · **端点在本机**（GEN_VL_BASE_URL 指向 127.0.0.1/::1/localhost）：
            #     平台一分钱不出，就不该占平台额度，连计数也不该记——`mat status`
            #     的视觉用量只该反映「平台代付」的那部分。
            # 第三条是 2026-10-26 补的：组合「GEN_PROMPT_MODE=server + 本地兜底端点」
            # 若不特判，本地兜底会把官方视觉额度吃光，额度满后 QuotaExceeded 连
            # **本地模型**一起拒掉——恰好废掉了本地化唯一确定的收益（截图不出本机）。
            if not is_loopback(vision.endpoint()):
                usage = UsageStore()
                enforce = promptstore.is_server()

                def before_call():
                    usage.check_vision(enforce=enforce)

        try:
            # P0-iOS 回归修复：此处曾依赖 drv.raw（Android 专属）——
            # WdaDriver 移除 raw 后视觉兜底在 iOS 上静默失效。改走中立截图方法。
            image = self.drv.screenshot_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            tree_xml = self.drv.dump()
            tree_hash = hashlib.md5(tree_xml.encode()).hexdigest()
            target_text = self._subst(step.target, variables).value
            cache_key = f"{tree_hash}:{step.id}:{target_text}"
            tree_evidence = bool(target_text) and target_text in tree_xml
            client = self.vision_client or vision.default_client()
            position = vision.locate(
                client, buffer.getvalue(), step.intent or "", target_text,
                cache_key=cache_key, tree_evidence=tree_evidence, before_call=before_call
            )
        except QuotaExceeded as exc:
            self.vision_reason = str(exc)
            return None
        except Exception as exc:
            # 网络错误/5xx/鉴权失败此前都被吞成"未命中或未启用"，
            # 用户会去查额度与 GEN_VL_FALLBACK，真因（API 超时/鉴权失败）被完全隐藏。
            self.vision_reason = f"调用异常: {type(exc).__name__}: {exc}"
            return None
        if position is None:
            # 调用过了但没采纳——把**模型侧的原因**带出来。此前这里恒报「未命中或
            # 未启用」，于是"模型说没找到""置信度不够被拒""压根没调"三件事在报告里
            # 长得一模一样（2026-10-26：本地 VL 跑完一条用例，无法判断它是否被调用）。
            self.vision_reason = vision.last_result().get("reason") or "模型未命中"
            return None
        return _Element(strategy="vision-llm", value=f"({position[0]},{position[1]})",
                        point=position, drv=self.drv)

    def _capture_logcat(self) -> str:
        """抓取失败步骤的**系统日志证据**（分类与归因的硬证据来源）。

        P2.2：改走驱动契约 `evidence_tail()`——Android 委托 `adb.logcat_tail`（单一实现，
        外部评审 P1-2 的纪律不变），iOS 取 syslog 环形缓冲。
        返回空串表示**没有证据源**（未装 pymobiledevice3 / 驱动未实现），不是"没有日志"。
        """
        try:
            return self.drv.evidence_tail() or ""
        except Exception:  # noqa: BLE001 —— 证据缺失不该改变执行结果
            return ""

    def _device_meta(self) -> dict:
        """设备身份（溯源用）。取不到返回空 dict——**不允许影响执行**。"""
        try:
            return self.drv.device_meta() or {}
        except Exception:  # noqa: BLE001 —— 溯源信息缺失不影响执行
            return {}

    def _tree_hash(self) -> str:
        return hashlib.md5(self.drv.dump().encode()).hexdigest()

    def _subst(self, loc: Locator, variables: dict[str, str]) -> Locator:
        # ⚠️ **`index` 必须搬过来**（2026-10-28 真机实测）：这里曾只重建
        # `strategy/value/fallback`，把 `index` 静默丢掉 ⇒ 用例写
        # `res-id=channel_bar_item_container, index=5`，运行期 `loc.index is None`
        # ⇒ `_find` 不传 `instance` ⇒ u2 命中**第一个**实例。
        # 症状是"动作没生效但也不报错"：`tap` 返回 passed（元素找得到、点得下去），
        # 点的是 index=0。tx 频道切换 3/3 全部停在 7x24，只有 `selected 国际` 断言抓到。
        # 与 `preconditions` 在 DSL 无处安放是同一类**静默丢字段**：新增字段先问
        # "这条重建路径上有没有人搬它"。
        return Locator(
            strategy=loc.strategy,
            value=substitute_vars(loc.value, variables),
            index=loc.index,
            fallback=self._subst(loc.fallback, variables) if loc.fallback else None,
        )

    def _dismiss_system_alerts(self) -> None:
        """每步开头清理盖住前台的弹窗（iOS）。

        能力边界（2026-10-19 真机定论）：
        - 挂在**本 App 会话**上的弹窗可以用 `drv.find` 点掉；
        - **系统弹窗归 SpringBoard 所有**，App 会话看不到它；而 WDA 建 springboard
          会话会激活/重启 SpringBoard —— 现象是「**黑屏 + 转圈（respring）→ 锁屏**」。
          这条路是自伤，已禁止（`WdaDriver.dismiss_system_alert` 不再建该会话）。
        - 所以真遇到系统弹窗时这里**如实报错**，把动作交给用户（同一 App 只需点一次）。
        """
        try:
            if self.drv.foreground_app().get("package") != "com.apple.springboard":
                return
        except Exception:  # noqa: BLE001 —— 前台查询失败就不折腾
            return
        # ⚠️ `foreground_app()` 返回 springboard **不代表** App 被挡住了：
        # 通知横幅/控制中心/来电横幅都归 SpringBoard，而 App 还在屏幕上、树照常可读。
        # 2026-10-19 真机实测：一条开发者通知横幅就让 42 条用例全部误判成"有浮层"。
        if self._app_on_screen(self._target_package()):
            return
        dismiss = getattr(self.drv, "dismiss_system_alert", None)
        if callable(dismiss):
            try:
                if dismiss():           # 只尝试 App 会话（便宜且无副作用）
                    _pause(0.4)
                    return
            except Exception:  # noqa: BLE001 —— 清弹窗失败不能影响执行
                pass
            raise RuntimeError(
                "有系统浮层盖住前台，两条常见原因请按屏幕对照处理："
                "① 授权弹窗（「允许…使用无线数据？」/通知/定位）——请在手机上点一次「允许」；"
                "② 「输入 iPhone 密码查看已存密码」——这是 iOS **自动填充密码**，"
                "需在 设置 → 密码 → 密码选项 关闭「自动填充密码」后重跑"
                "（自动化无法输入锁屏密码，关了它之后登录类用例才能跑）。"
            )
        # 旧驱动/桩：保留原逐标签兜底（行为不变）
        for label in ("不允许", "Don't Allow", "暂不", "好", "OK"):
            try:
                node = self.drv.find("text", label, 0.6)
                if getattr(node, "exists", False):
                    node.click()
                    _pause(0.4)
                    return
            except Exception:  # noqa: BLE001 —— 逐个尝试，失败就试下一个
                continue

    def _app_on_screen(self, package: str | None) -> bool:
        """App 是否真的在屏幕上（驱动不支持时返回 False，保持原行为）。"""
        on_screen = getattr(self.drv, "app_on_screen", None)
        if not callable(on_screen):
            return False
        try:
            return bool(on_screen(package))
        except Exception:  # noqa: BLE001 —— 探测语义：问不出来就当不在
            return False

    def _wait_landed(self, package: str | None, tries: int) -> bool:
        """等被测 App 真的到前台（每拍 0.5s，最多 `tries` 拍）。

        `foreground_app()` 不可全信：iOS 的通知横幅、控制中心、来电横幅都归
        SpringBoard，App 明明还在屏幕上，它却返回 `com.apple.springboard`
        （2026-10-19 真机：一条开发者通知横幅就让 42 条用例全判成「有系统浮层」）。
        所以每 3 拍（1.5s）用一次 `app_on_screen` 兜底——它看的是"能不能读到
        App 的控件树"，代价约 0.5~1s，不能每拍都做。
        """
        for i in range(tries):
            try:
                if not package or self.drv.foreground_app().get("package") == package:
                    return True
            except Exception:  # noqa: BLE001 —— 探测语义
                pass
            if i % 3 == 2 and self._app_on_screen(package):
                return True
            _pause(0.5)
        # 最后一拍再兜一次：慢机型可能刚好赶上
        return self._app_on_screen(package) if package else False

    def _assert_foreground(self, package: str | None, tries: int = 12) -> None:
        """Android launch 后的前台守卫：目标 App 没到前台就**当场失败**。

        病灶（2026-10-25 真机实测）：同一用例 15 轮连跑，3 轮设备跑到系统「设置」，
        而 `s1 launch` 照样判 passed —— Android 分支只发启动命令，**从不看前台**
        （`_wait_landed` 原先只在 iOS 分支里用）。错误于是被推到下一步，并被归因成
        `locate-fail`（"定位失败"），真实原因（设备在别的 App 里）被彻底埋掉。

        取舍：前台取不到（`foreground_app` 抛错/返回空）时**不判死** —— 宁可漏报，
        也不制造"设备好好的却被判导航失败"的假失败。
        """
        if not package:
            return
        if self._wait_landed(package, tries=tries):
            return
        try:
            actual = (self.drv.foreground_app() or {}).get("package") or ""
        except Exception:  # noqa: BLE001 —— 问不出来就不判死
            actual = ""
        if not actual:
            return
        raise RuntimeError(
            f"启动后前台不是目标 App：期望 {package}，实际 {actual}"
            f"（{tries * 0.5:.0f}s 内未回到目标；常是弹窗/上一个任务把它顶掉了）")

    def _safe_dump(self) -> str:
        """取一次控件树；失败返回空串（噪声清理是 best-effort，不给用例抛异常）。"""
        try:
            return self.drv.dump() or ""
        except Exception:  # noqa: BLE001 —— 拿不到树由调用方回退
            return ""

    def _click_node(self, node: Node) -> bool:
        """点 dump 里挑出来的节点；返回是否点到。

        优先按**边界中心坐标**点，这与 u2 的 `node.click()` 等价（它点的就是 bounds
        中心），但省掉一次"元素查询 + 等待"的往返。bounds 退化（宽或高为 0，典型是
        `[0,0][0,0]`，点下去是状态栏）或坐标点击失败时，退回按**完整文案精确查找**——
        文案来自刚取的 dump，正常情况下一次就中。
        """
        box = rect(node)
        if box and box[2] > box[0] and box[3] > box[1]:
            try:
                self.drv.click((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
                return True
            except Exception:  # noqa: BLE001 —— 坐标点击失败，退回元素级
                pass
        try:
            target = self.drv.find("text", node.text, 0.5)
            if getattr(target, "exists", False):
                target.click()
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _click_noise_by_find(self, labels: tuple[str, ...], contains: bool,
                             timeout: float) -> bool:
        """逐条 `find` 的旧路径；返回是否点到。

        现在只用于两处：① iOS —— WDA 的 dump 是 `XCUIElementType*` 树，`parse_nodes`
        只认 `<node>`，本地匹配用不了；② dump 拿不到树的兜底（老设备/传输抖动）。
        超时与改动前保持一致（权限向 0.8s、广告向 1.0s），行为零漂移。
        """
        by = "textContains" if contains else "text"
        for label in labels:
            try:
                node = self.drv.find(by, label, timeout)
                if getattr(node, "exists", False):
                    node.click()
                    return True
            except Exception:  # noqa: BLE001 —— 逐个尝试，失败就试下一个
                continue
        return False

    def _click_launch_noise(self) -> bool:
        """Android 启动噪声：一次 dump 内全量匹配，命中才点；返回是否点到东西。

        为什么不再逐条 `find`（2026-10-25 真机实测单次 14.0–14.4s）：10 个候选文案
        = 10 次 `find`，而**每次未命中都要在设备侧轮询到超时**（0.8s / 1.0s）——
        于是"页面根本没有广告"这个最常见的情况反而最慢。10 条用例白扔 140s，且
        任何日志都看不出这些时间花在哪（都不是错误，只是超时）。
        改成 dump 一次（~0.3s）拿整棵树、在本地按同一优先级顺序挑控件：未命中是
        纯内存判断，零设备往返。
        """
        xml = self._safe_dump()
        if not xml:
            # dump 拿不到 ⇒ 退回逐条 find：宁可慢，不能丢功能
            clicked = self._click_noise_by_find(_PERM_DENY_LABELS, contains=False,
                                                timeout=0.8)
            return self._click_noise_by_find(_SKIP_LABELS, contains=True,
                                             timeout=1.0) or clicked
        nodes = _parse_nodes_safe(xml)
        clicked = False
        # ① 权限/确认弹窗的否定向：**精确** text（contains 会把「取消」打中「确定取消」）
        perm = _pick_label(nodes, _PERM_DENY_LABELS, contains=False)
        if perm is not None and self._click_node(perm):
            clicked = True
            _pause(0.5)
            nodes = _parse_nodes_safe(self._safe_dump())  # 弹窗关掉，页面已变，重新取树
        # ② 开屏广告：textContains（真机文案常带倒计时）；晚到则按信号重试
        for attempt in range(_SPLASH_RETRY_ATTEMPTS):
            skip = _pick_label(nodes, _SKIP_LABELS, contains=True)
            if skip is not None:
                if self._click_node(skip):
                    return True
                break  # 挑到了却点不动：拿同一棵树重试没有意义
            if attempt + 1 >= _SPLASH_RETRY_ATTEMPTS or not _splash_likely(nodes):
                break
            _pause(_SPLASH_RETRY_PAUSE)
            nodes = _parse_nodes_safe(self._safe_dump())
        return clicked

    def _skip_splash_ad(self, package: str | None = None) -> None:
        """启动噪声清理（best-effort）：开屏广告跳过 + 残留权限弹窗；不阻塞、不报错。

        C1 同源修正（2026-10-12 评审）：旧实现判据 `raw is None ⇒ 桩` 在 raw 删除后
        对真机恒成立，整段跳过逻辑成了死代码。改走中立定位 API `drv.find`。
        2026-10-13 扩展（发起人「4 用例全栽在跳过广告但广告已消失」）：① 不再限于
        cold 启动——**每次 launch 后都清**（热启动时广告 find 不中是 no-op，换用例
        不再为前置噪声写易碎步骤）；② 叠加权限弹窗清理（复用重建索引的
        force-stop permissioncontroller 逻辑，残留弹窗挡前台是老问题）。
        2026-10-25 提速（14.0–14.4s/次 → 见 `_click_launch_noise`）：Android 走
        「一次 dump 本地匹配」，iOS 仍走 find 链（树不是 `<node>`）。
        代价说清：晚到广告的观察窗口从"被 10 次 find 超时意外撑出来的 ~10s"缩到
        「0.6s 首探 + 两次信号驱动重试」；只有在 dump 出现广告线索词、或页面标签
        稀疏（<8，典型开屏/加载骨架）时才继续等，正常业务页一次即止。
        """
        _pause(_SPLASH_SETTLE)  # 等开屏渲染
        try:
            from .page_index import _dismiss_permission_dialog
            _dismiss_permission_dialog(self.drv)
        except Exception:
            pass
        # iOS 系统权限弹窗（2026-10-18 实测 / 2026-10-19 修）：重装后首次启动会弹
        # 「允许"MAT 靶场"使用无线数据？」，弹窗由 springboard 持有 ⇒ App 会话找不到。
        # ⚠️ 但**不能**看到 foreground 是 springboard 就去建 springboard 会话：
        # 刚 launch 的 1~2s 内前台本来还是 springboard，建会话会激活 SpringBoard、
        # 把被测 App 顶回桌面（实测：批量首条 launch 通过、下一步截图就是主屏幕）。
        # 判据改成「**等 App 真的到前台**」：等不到才认定是弹窗，清完再把 App 拉回来。
        dismiss = getattr(self.drv, "dismiss_system_alert", None)
        if callable(dismiss):
            landed = self._wait_landed(package, tries=12)
            if not landed:
                # 只尝试 App 会话（无副作用）。**绝不建 springboard 会话**：
                # 那会 respring（黑屏+转圈）并把设备落到锁屏——实测踩过。
                try:
                    dismiss()
                except Exception:  # noqa: BLE001 —— 清弹窗 best-effort
                    pass
                _pause(0.5)
                landed = self._wait_landed(package, tries=4)
                if not landed:
                    raise RuntimeError(
                        "启动后 App 一直不在前台。按屏幕对照："
                        "① 「允许…」授权弹窗——在手机上点一次「允许」；"
                        "② 「输入 iPhone 密码查看已存密码」——iOS 自动填充密码，"
                        "请在 设置 → 密码 → 密码选项 关闭「自动填充密码」；"
                        "③ 都没有——App 可能启动即崩溃，用 `mat ios-setup` 与 syslog 证据排查。"
                    )
            self._click_noise_by_find(_SKIP_LABELS, contains=True, timeout=1.0)
            _pause(_SPLASH_TAIL)
            return
        if self._click_launch_noise():
            # 跳过后的页面切换；没点就**不等**——下一步的 wait_stable_xml 自会判定空闲
            _pause(_SPLASH_TAIL)

    def _run_api_step(self, step, variables: dict[str, str]) -> StepResult:
        """P1-1：api 步骤执行（纯网络，无设备交互）。

        - 证据进 StepResult.api（run.json/报告持久化，头已打码、响应体截断）；
        - 失败 category="api"（区别于定位/断言/环境，报告独立配色）；
        - save 提取的变量写回运行时变量表，后续步骤以 ${var} 引用（同 dict 对象原地更新）；
        - api_client.run_api 契约是"永不抛"，这里只做防御性兜底。
        """
        from . import api_client

        result = StepResult(step_id=step.id, intent=step.intent,
                            action=step.action.value)
        params = step.params or {}
        try:
            evidence = api_client.run_api(params, variables)
        except Exception as exc:  # noqa: BLE001 —— 防御：证据兜底，不让契约外的异常裸奔
            evidence = {"method": str(params.get("method", "GET")),
                        "url": str(params.get("url", "")),
                        "status": None, "duration_ms": 0, "passed": False,
                        "error": f"{type(exc).__name__}: {exc}"}
        result.api = evidence
        result.duration_ms = int(evidence.get("duration_ms") or 0)
        # save 提取的变量已由 api_client.run_api 原地写回 variables（单一写径，见该函数）
        if evidence.get("passed"):
            result.status = "passed"
        else:
            result.status = "failed"
            result.category = "api"
            result.category_source = "rules"
            result.error = evidence.get("error") or "API 步骤失败（明细见报告的接口证据区块）"
        return result

    def _act(self, step, case: TestCase, element: Element | None, variables: dict[str, str]) -> None:
        # params 里显式给 null 的值不转成字符串 "None"（2026-10-12 评审🔵）：
        # 此前 params={"text": None} 会往输入框打字"None"。
        params = {k: (None if v is None else substitute_vars(str(v), variables))
                  for k, v in step.params.items()}
        action = step.action
        if action == Action.LAUNCH:
            package = params.get("package") or case.target.get("package", "")
            if not package:
                raise ValueError("launch 需要 package 参数或用例 target.package")
            # P1-1 兜底（2026-10-24）：dsl 边界已校验静态值，但 params 会先过
            # substitute_vars——变量替换可能带出模型层看不到的脏值，拼 adb shell 前再收口一次。
            from .pkgname import safe_component, safe_package_name

            safe_package_name(package)
            activity = params.get("activity")
            if activity:
                safe_component(activity)
            self._launched_package = package
            if _cold_start_enabled(params):
                # 冷启动：先强制停止，保证用例从已知初始态开始（消除跨用例状态污染）
                try:
                    self.drv.app_stop(package)
                except Exception:
                    pass
            started = False
            # `am_start -W` 是 **adb 命令**：iOS 上传了 serial 会白跑一次 adb 并失败
            # （真机实测 launch 多花 1-2s）。按平台门控。
            if getattr(self.drv, "platform", "android") == "android" \
                    and getattr(self.drv, "serial", None):
                # am start -W：启动同时采集启动耗时（perf.py）；失败回退 u2.app_start
                perf_result = am_start_w(self.drv.serial, package, activity)
                if perf_result:
                    started = True
                    if perf_result.get("launch_ms") is not None:
                        self.perf["launch_ms"] = perf_result["launch_ms"]
            if not started:
                self._with_transport_retry(lambda: self.drv.app_start(package, activity))
            # 每次拉起后都清启动噪声（2026-10-13 扩展，原仅 cold）：广告/权限弹窗是
            # 引擎职责而非用例职责——用例里「跳过广告」类步骤因此可标 continue 甚至省略
            self._skip_splash_ad(package)
            # P0 导航链守卫（2026-10-25）：Android 此前只发启动命令、从不校验前台，
            # 设备被带到别的 App 也判 passed，错误全部转嫁给下一步（并误标 locate-fail）。
            if getattr(self.drv, "platform", "android") == "android":
                self._assert_foreground(package)
            if step.target is not None:
                # 启动后校验锚点：定位不到会抛 LocateFailed（这才是 launch 该有的失败语义）
                resolve(self.drv, self._subst(step.target, variables), step.timeout_ms)
        elif action == Action.TAP:
            # 输入后要点击别处：先收键盘（2026-10-19 真机）。
            # iOS 上输入后软键盘会把页面顶上去，**而 WDA 报的元素 frame 不跟着更新**，
            # `element.tap()` 就点到旧位置 —— 表单用例「填姓名→点提交」失败在
            # 断言「已提交」上，截图里「提交」还被键盘压着。用户遇到这种情况也会
            # 先收键盘再点，所以这里等价处理（只有 input→tap 这种序列才做，代价约 0.3s）。
            if self._input_since_tap:
                self._input_since_tap = False
                hide = getattr(self.drv, "dismiss_keyboard", None)
                if callable(hide):
                    try:
                        hide()
                    except Exception:  # noqa: BLE001 —— 收不起来也要继续点
                        pass
                    _pause(0.3)
            element.click()
        elif action == Action.LONG_PRESS:
            element.long_click()
        elif action == Action.INPUT:
            element.set_text(params.get("text") or "")
            self._input_since_tap = True
        elif action == Action.CLEAR:
            element.clear_text()
        elif action in (Action.SWIPE, Action.SCROLL):
            self.drv.swipe_ext(params.get("direction", "up"))
        elif action == Action.BACK:
            self.drv.press("back")
        elif action == Action.HOME:
            self.drv.press("home")
        elif action == Action.KEY_EVENT:
            self.drv.press(params.get("key", "back"))
        elif action == Action.WAIT:
            # ms 非法值容错（2026-10-12 评审🔵）：DSL 宽松接受 {"ms":"abc"}，此前直接
            # ValueError → 整步归 unknown。走 _pause 注入点（conftest no-op），测试提速。
            try:
                ms = int(params.get("ms", 1000))
            except (TypeError, ValueError):
                ms = 1000
            _pause(max(0.0, ms / 1000))
        else:
            raise ValueError(f"未实现动作: {action.value}")

    def _check_assertions(self, step, variables: dict[str, str], timeout_ms: int = 3000) -> None:
        for assertion in step.assertions:
            if assertion.target is None:
                raise AssertionError(f"断言 {assertion.type.value} 缺少 target")
            # 用 try_resolve 而不是 resolve：`not-exists` 的正常情况就是找不到，
            # 若让它抛 LocateFailed，该断言永远无法成立，且失败会被误分类成 locate-fail。
            element = try_resolve(self.drv, self._subst(assertion.target, variables), timeout_ms)
            exists = element is not None
            if assertion.type == AssertionType.EXISTS and not exists:
                raise AssertionError(f"断言失败: 控件应存在: {assertion.target.value}")
            if assertion.type == AssertionType.NOT_EXISTS:
                if exists:
                    raise AssertionError(f"断言失败: 控件不应存在: {assertion.target.value}")
                continue
            if assertion.type in (AssertionType.TEXT_EQUALS, AssertionType.TEXT_CONTAINS):
                if not exists:
                    raise AssertionError(f"断言失败: 控件未找到，无法校验文本: {assertion.target.value}")
                actual = element.get_text() or ""
                expected = substitute_vars(assertion.expected or "", variables)
                if assertion.type == AssertionType.TEXT_EQUALS and actual != expected:
                    raise AssertionError(f"断言失败: 期望文本 {expected!r}，实际 {actual!r}")
                if assertion.type == AssertionType.TEXT_CONTAINS and expected not in actual:
                    raise AssertionError(f"断言失败: 期望包含 {expected!r}，实际 {actual!r}")
                continue
            if assertion.type in (AssertionType.ENABLED, AssertionType.DISABLED,
                                  AssertionType.CHECKED, AssertionType.SELECTED):
                # 控件状态断言（T36）："按钮该禁用了没禁用"这一类交互缺陷的直接断言。
                # disabled = enabled 状态取反；其余与 u2 info 同名同义。
                if not exists:
                    raise AssertionError(f"断言失败: 控件未找到，无法校验状态: {assertion.target.value}")
                state_key = "enabled" if assertion.type == AssertionType.DISABLED else assertion.type.value
                try:
                    raw = element.get_state(state_key)
                except RuntimeError as exc:
                    raise AssertionError(f"断言失败: {exc}") from exc
                actual = (not raw) if assertion.type == AssertionType.DISABLED else raw
                if not actual:
                    raise AssertionError(
                        f"断言失败: 控件状态应为 {assertion.type.value}: {assertion.target.value}")
                continue
