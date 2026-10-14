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
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .dsl import Action, AssertionType, Locator, LocatorStrategy, OnFailure, TestCase, dump_dsl
from .locator import Element, LocateFailed, heal_by_hints, heal_fuzzy, resolve, try_resolve
from .perf import am_start_w, mem_pss_mb
from .state import state_home as _state_home


# 可注入的暂停（外部评审 P1-7）：测试替换为 no-op，全量测试 <90s
def _pause(seconds: float) -> None:
    time.sleep(seconds)

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
)

CATEGORIES = ("app-crash", "env-failure", "assert-fail", "locate-fail", "unknown")


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
    if "fatal exception" in log or "anr in " in log:
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
    # 分类来源：None/"rules"=内置规则判定；"human"=**人工校准改判过**（见 calibration.py）。
    # 报告要能标出来 —— 否则人看不出这条结论是自己判的、还是机器判的。
    category_source: str | None = None
    category_before_calibration: str | None = None  # 改判前的原始分类（留痕，便于回溯）
    retried: bool = False  # post-verify 无变化后的动作级重试
    retried_on_failure: bool = False  # on_failure=retry 的整步重试
    heal: dict | None = None  # 自愈提案 {"original": ..., "match": ...}，待人工确认回写
    warning: str | None = None
    error: str | None = None
    screenshot: str | None = None  # 相对 artifacts 目录
    logcat_file: str | None = None  # 相对 artifacts 目录
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
    steps: list[StepResult] = Field(default_factory=list)
    operator: str = ""  # 执行人（web_api 注入触发者名字；CLI 直跑为空）——run.json/报告落盘


class Runner:
    def __init__(self, drv, artifacts_dir: Path, cred_store=None, history_path: Path | None = None,
                 vision_client=None, index_path: Path | None = None, recorder=None):
        self.drv = drv
        self.artifacts = Path(artifacts_dir)
        self.recorder = recorder # 录屏器：None=不录；接口 start/stop/keep/discard
        (self.artifacts / "screenshots").mkdir(parents=True, exist_ok=True)
        self.cred_vars = cred_store.load_as_vars() if cred_store else {}
        self.history_path = (Path(history_path) if history_path
                             else _state_home() / "history.jsonl")
        self.vision_client = vision_client  # 注入则忽略 GEN_VL_MODEL 门控（单测用）
        # 页面图谱路径：显式传入优先；不传则按工作区约定 <cwd>/artifacts/page_index/<包名>.json
        self.index_path = Path(index_path) if index_path else None
        self.vision_reason: str = ""  # 最近一次 vision 兜底未命中的原因（额度用尽/未命中）
        self.perf: dict = {}
        self.operator = ""  # 执行人（web_api 层注入 operator_of(body)；CLI 直跑留空）

    def run(self, case: TestCase, on_step=None, cancel_event=None) -> RunResult:
        """执行用例。on_step(StepResult) 每步定稿时回调（监控旁路，异常被吞不影响执行）；
        cancel_event 置位后在**下一步开始前**安全退出（不打断当前步，
        已完成步骤的证据照常保留，status="cancelled"）。"""
        started = time.monotonic()
        variables = {**self.cred_vars, **case.vars}  # ${cred.名称} 来自凭据存储；用例 vars 同名可覆盖
        self._wake_device()
        if self.recorder:
            self.recorder.start() # ：全程录，失败保留/通过删除（stop 在 finally）
        # **每轮清一次 logcat**（T76）：崩溃证据靠 logcat，而窗口开到了整段缓冲区
        # （`LOGCAT_TAIL_LINES`）⇒ 上一轮留下的崩溃会让这一轮的**无关失败**被误判成
        # `app-crash`（正是本项目最怕的假阳性）。清一次就把时效限定在本轮。
        from .adb import logcat_clear

        logcat_clear(self.drv.serial)
        results: list[StepResult] = []
        cancelled = False
        for idx, step in enumerate(case.steps, 1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
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
            # 可选步骤（onFailure=continue，2026-10-13）：定位失败 ⇒ 目标没出现（典型：
            # 启动广告已消失/弹窗没弹）——记 skipped 继续，不算失败不污染通过率；
            # 其他异常保持 failed（真实问题要露头），但同样不触发 abort。
            if (result.status == "failed" and step.on_failure == OnFailure.CONTINUE
                    and "LocateFailed" in (result.error or "")):
                result.status = "skipped"
                result.error = f"可选步骤（onFailure=continue）：{result.error}；目标未出现，已继续"
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
            status="cancelled" if cancelled else ("failed" if failed else "passed"),
            step_success_rate=rate,
            duration_ms=int((time.monotonic() - started) * 1000),
            failed_categories=categories,
            perf=self.perf,
            preconditions=list(case.preconditions),
            operator=self.operator,
            steps=results,
        )
        self._persist(case, run_result)
        status = run_result.status
        if self.recorder:
            files = self.recorder.stop()
            if files:
                # 通过 ⇒ 删（磁盘纪律）；失败/中止 ⇒ 挪进 artifacts/video/（报告相对引用）
                (self.recorder.discard(files) if status == "passed"
                 else self.recorder.keep(files, self.artifacts))
        return run_result

    def run_datasets(self, case: TestCase, root: Path | None = None,
                     on_step=None, cancel_event=None) -> list[tuple[str, RunResult, Path]]:
        """数据驱动（CASE-06）：每个数据组独立执行一轮，返回 [(组名, 结果, 产物目录)]。

        变量合并优先级：数据组 vars > 用例 vars > 凭据；各组产物独立目录。
        on_step / cancel_event 透传给每轮 run（监控与中止对数据驱动同样生效）。
        """
        datasets = case.datasets or [{"name": "默认"}]
        results = []
        base_root = Path(root) if root else self.artifacts
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
            (artifacts / "screenshots").mkdir(parents=True, exist_ok=True)
            results.append((name, self.run(merged_case, on_step=on_step, cancel_event=cancel_event), artifacts))
        return results

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
            "app_version": app_version(self.drv, target_pkg),
            # **安装包指纹**：所有靶场变体共用 `versionName=1.0` ⇒ 光看版本号
            # 分辨不出 base / v09-defect-free，指纹才能回答"测的是哪个构建"。
            "app_apk_sha256": app_apk_sha256(self.drv, target_pkg),
            "case": json.loads(dump_dsl(case)),
            "run": run_payload,
        }
        (self.artifacts / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
            path = Path.cwd() / "artifacts" / "page_index" / f"{package}.json"
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

    def _run_step_once(self, idx: int, step, case: TestCase, variables: dict[str, str],
                       attempt: int = 1) -> StepResult:
        t0 = time.monotonic()
        result = StepResult(step_id=step.id, intent=step.intent, action=step.action.value)
        # 文件名带 attempt 后缀——retry 二次执行此前会覆盖
        # 首跑同名截图，"每步留证据"丢的正是"第一次为什么失败"的那张图。
        shot = self.artifacts / "screenshots" / f"step_{idx:02d}_{step.id}_a{attempt}.png"
        logcat_path = self.artifacts / "logs" / f"step_{idx:02d}_{step.id}_a{attempt}.logcat.txt"

        try:
            stable, xml_before = wait_stable_xml(self.drv)

            element: Element | None = None
            if step.target is not None:
                element = resolve(self.drv, self._subst(step.target, variables), step.timeout_ms)
                result.used_strategy = element.strategy

            before = hashlib.md5(xml_before.encode()).hexdigest()  # 复用空闲判定的最后一次 dump
            self._act(step, case, element, variables)
            _pause(0.3)

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
                        result.warning = ("输入类动作后控件树无变化——**不自动重放**（避免重复输入/覆盖字段）；"
                                          "若文本未生效请检查输入框焦点与软键盘状态")

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
                labels = [str(c.get("text") or c.get("desc") or "")[:20]
                          for c in digest_page(self.drv.dump(), pkg)["controls"]]
                labels = [x for x in labels if x][:8]
                if labels:
                    # 归因提示**追加不覆盖**——此前无条件赋值会
                    # 挤掉同字段里更有价值的 post-verify 告警（树无变化/键盘遮挡）。
                    hint = f"当前页面可见控件: {'、'.join(labels)}"
                    result.warning = f"{result.warning}；{hint}" if result.warning else hint
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
                        self._act(step, case, healed, variables)
                        _pause(0.3)
                        after_heal = self._tree_hash()  # 自愈动作同样做 post-verify
                        self._check_assertions(step, variables)
                        result.status = "passed"
                        result.error = None
                        result.used_strategy = healed.strategy
                        result.warning = f"定位失败已由 {via} 兜底命中，提案待人工确认回写"
                        if after_heal == before_heal:
                            result.warning += "；注意：自愈动作后控件树无变化"
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
                # 人工校准规则叠在内置判定之上（`calibration/rules.json`）——
                # 「校准一次，下次不再犯」就落在这里。规则表读不动时 `load_rules` 返回空表，
                # 退化成纯内置判定（**不许因为规则坏了而影响执行**）。
                from .calibration import load_rules
                result.category = classify_failure(
                    result.error or "", logcat_text, load_rules(Path.cwd()))
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
        return result

    def _vision_locate(self, step, variables) -> Element | None:
        """vision-llm 兜底定位：截图+意图 → VL 模型 → 像素坐标（置信度红线 0.7）。

        免费档按免费档月上限做额度核销；额度耗尽时**优雅降级**（等同未启用），
        步骤退回定位失败而不中断本地执行，原因写入 self.vision_reason 供失败信息展示。
        """
        from . import vision
        from .locator import Element as _Element
        from .quota import QuotaExceeded, UsageStore

        self.vision_reason = "未命中或未启用"
        if not (self.vision_client or vision.enabled()):
            return None
        if step.target is None:
            return None

        # 注入客户端（单测）不计配额，保持测试无副作用；环境配置路径按免费档上限核销
        before_call = None
        if self.vision_client is None:
            usage = UsageStore()

            def before_call():
                usage.check_vision()

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
            return None
        return _Element(strategy="vision-llm", value=f"({position[0]},{position[1]})",
                        point=position, drv=self.drv)

    def _capture_logcat(self) -> str:
        """抓取设备 logcat 末段作为失败证据（失败分类与归因的硬证据来源）。无 adb 则静默跳过。

        走 adb.logcat_tail 单一实现（外部评审 P1-2：此前 runner 自建 subprocess 版，
        超时/失败语义与 adb.py 漂移）。"""
        from .adb import logcat_tail

        # 用默认窗口（整段缓冲区 + Error 以上）——写死 200 会让崩溃被噪声挤出窗口（T76）。
        return logcat_tail(self.drv.serial)

    def _tree_hash(self) -> str:
        return hashlib.md5(self.drv.dump().encode()).hexdigest()

    def _subst(self, loc: Locator, variables: dict[str, str]) -> Locator:
        return Locator(
            strategy=loc.strategy,
            value=substitute_vars(loc.value, variables),
            fallback=self._subst(loc.fallback, variables) if loc.fallback else None,
        )

    def _skip_splash_ad(self) -> None:
        """启动噪声清理（best-effort）：开屏广告跳过 + 残留权限弹窗；不阻塞、不报错。

        C1 同源修正（2026-10-12 评审）：旧实现判据 `raw is None ⇒ 桩` 在 raw 删除后
        对真机恒成立，整段跳过逻辑成了死代码。改走中立定位 API `drv.find`。
        2026-10-13 扩展（发起人「4 用例全栽在跳过广告但广告已消失」）：① 不再限于
        cold 启动——**每次 launch 后都清**（热启动时广告 find 不中是 no-op，成本 ~2.5s，
        换用例不再为前置噪声写易碎步骤）；② 叠加权限弹窗清理（复用重建索引的
        force-stop permissioncontroller 逻辑，残留弹窗挡前台是老问题）。
        """
        _pause(1.5)  # 等开屏渲染
        try:
            from .page_index import _dismiss_permission_dialog
            _dismiss_permission_dialog(self.drv)
        except Exception:
            pass
        for label in ("跳过", "跳过广告", "关闭", "以后再说"):
            try:
                node = self.drv.find("textContains", label, 1.0)
                if getattr(node, "exists", False):
                    node.click()
                    break
            except Exception:
                continue
        _pause(1)

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
            if params.get("cold") in (True, "true", "True"):
                # 冷启动：先强制停止，保证用例从已知初始态开始（消除跨用例状态污染）
                try:
                    self.drv.app_stop(package)
                except Exception:
                    pass
            started = False
            if getattr(self.drv, "serial", None):
                # am start -W：启动同时采集启动耗时（perf.py）；失败回退 u2.app_start
                perf_result = am_start_w(self.drv.serial, package, params.get("activity"))
                if perf_result:
                    started = True
                    if perf_result.get("launch_ms") is not None:
                        self.perf["launch_ms"] = perf_result["launch_ms"]
            if not started:
                self.drv.app_start(package, params.get("activity"))
            # 每次拉起后都清启动噪声（2026-10-13 扩展，原仅 cold）：广告/权限弹窗是
            # 引擎职责而非用例职责——用例里「跳过广告」类步骤因此可标 continue 甚至省略
            self._skip_splash_ad()
        elif action == Action.TAP:
            element.click()
        elif action == Action.LONG_PRESS:
            element.long_click()
        elif action == Action.INPUT:
            element.set_text(params.get("text") or "")
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
