"""`mat verify`：一次调用拿到**机器可判定的结论**（协议 `mat.verify/1`）。

给谁用：AI 编码 agent 与 CI。动机（2026-10-30）：

- `mat run` 是面向人的：输出几十行文本、退出码有语义但没写成契约；
- agent 要的是「一次调用 → 结构化 verdict → 决定修代码还是重跑」，不该去解析终端文本；
- 失败分类、证据路径、步骤状态**本来就在产物里**，缺的只是一个稳定的外壳。

## 边界（刻意不做）

- **不生成用例**：verify 只负责验证。生成要走 `mat gen`（消耗额度、语义完全不同）。
- **不另立退出码**：沿用 `mat run` 的约定（0 通过 / 2 有失败 / 3 前置条件拒跑 /
  4 未跑完 / 1 运行错误），见使用手册。检测到「跑通了但码不对」时如实进 `notes`，
  不偷偷改码——两套码会让 CI 与 agent 各判一套。
- **不做增量选择**：黑盒 UI 测试无法从 git diff 推「受影响用例」，硬做是假精度
  （跑错的用例 + 漏掉的用例，比全量更危险）。

## 协议字段

`schema` / `verdict` / `exit_code` / `ok` / `case` / `package` / `steps` /
`failed_categories` / `failed_steps` / `next_actions` / `artifacts` / `notes`。

`verdict` 五态：`pass` / `fail` / `blocked`（前置条件拒跑）/ `incomplete`（未跑完）/
`error`（设备或运行错误）。**`ok` 只在 `pass` 时为 true**，别用 exit_code 自己推。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "mat.verify/1"

#: 退出码（与 `mat run` 同一套，勿另立）
EXIT_PASS = 0
EXIT_ERROR = 1
EXIT_FAIL = 2
EXIT_BLOCKED = 3
EXIT_INCOMPLETE = 4

_VERDICT_BY_EXIT = {EXIT_PASS: "pass", EXIT_FAIL: "fail",
                    EXIT_BLOCKED: "blocked", EXIT_INCOMPLETE: "incomplete"}


def verdict_of(exit_code: int) -> str:
    return _VERDICT_BY_EXIT.get(exit_code, "error")


#: 失败分类 → **下一步动作**（agent 自修闭环靠这张表）。
#:
#: 为什么必须给动作而不是解释：agent 拿到「locate-fail」不知道修什么；写成动作
#: （先看哪份证据、再决定改断言还是改定位）它才能自己往下走。人工看报告也知道
#: 该先翻哪个文件。分类取值见 `runner.CATEGORIES`，两处必须同步。
NEXT_ACTIONS: dict[str, str] = {
    "locate-fail": "定位没找到目标控件。先看本步证据里的控件树（screens/step_*_*.json）"
                   "核对文案与 res-id 是否变了；变了就更新用例，没变则是页面没到，"
                   "先修前置条件再重跑。",
    "assert-fail": "步骤执行到了但断言不成立。对照截图确认当前页是不是用例假设的那一页；"
                   "若页面确实错了，修步骤；若页面对了，是断言写错（改成 exists/不显示/相等）。",
    "env-failure": "环境不满足（设备断开/App 不在前台/锁屏）。**先修环境再重跑**，"
                   "不要改用例——这类失败改用例只会把问题藏起来。",
    "app-crash": "被测应用崩溃。取 logcat 按堆栈修代码，这是产品缺陷不是用例问题；"
                 "修完再重跑同一批验证崩溃是否消失。",
    "api": "接口步骤失败。核对 method/url/期望状态码与鉴权头；证据在 run.json 的步骤 api 段。",
    "unknown": "未能归类。先看截图与控件树，再决定补断言还是补定位；"
               "同类连续出现说明规则该扩了，记进用例缺陷而不是反复重跑。",
    # 拒跑两种子原因（`blocked_reason`）各给一条——拒跑是最需要"下一步做什么"的结论，
    # 只写在 notes 里 agent 读不到（它优先读 next_actions）。
    "precondition": "前置条件 MAT 改不了（系统权限/授权态、账号登录态）。先手工把设备置成该"
                    "状态、再加 --allow-unmet-preconditions 放行本次；或删掉该前置、让用例"
                    "自己用步骤到达——别把它当定位问题查（那样查的是假失败）。",
    "device-busy": "设备被控制台任务或另一进程占着（排队超时）。等它跑完或换一台设备；"
                   "不要改用例——两个进程抢同一台手机产出的结果无效。",
    "install": "安装没成功，用例**一条都没跑**。看 install.error：包名不符就核对 "
               "--package 或用例的 target.package（这种情形下根本没安装，设备未被改动）；"
               "被系统拒绝就查签名/存储空间/未知来源权限。不要改用例步骤——它还没被执行过。",
}


def _rel(artifacts: Path, value: str | None) -> str | None:
    """证据路径统一成「相对产物目录」——绝对路径换台机器就失效。"""
    if not value:
        return None
    p = Path(value)
    try:
        return p.relative_to(artifacts).as_posix()
    except ValueError:
        return p.name


def build_verdict(payload: dict, exit_code: int, artifacts: Path,
                  report: Path | None = None) -> dict:
    """把一次执行结果摊成 `mat.verify/1`。

    `payload` 就是 `RunResult.model_dump()`（或 run.json 里的 `run` 段）——这样同一套
    判定既能用于刚跑完的结果，也能用于**已有产物**（回归回溯/重判），不必为两种来源
    写两份逻辑。
    """
    artifacts = Path(artifacts)
    steps = payload.get("steps") or []
    failed_steps = []
    for s in steps:
        if s.get("status") == "passed":
            continue
        failed_steps.append({
            "step_id": s.get("step_id"),
            "action": s.get("action"),
            "status": s.get("status"),
            "intent": s.get("intent") or "",
            "category": s.get("category"),
            "error": (s.get("error") or "").splitlines()[0][:300] if s.get("error") else "",
            "off_page": s.get("off_page"),
            "false_red": bool(s.get("false_red")),
            "evidence": {
                "screenshot": _rel(artifacts, s.get("screenshot")),
                "screen": _rel(artifacts, s.get("screen")),
                "logcat": _rel(artifacts, s.get("logcat_file")),
            },
        })

    categories = dict(payload.get("failed_categories") or {})
    verdict = verdict_of(exit_code)
    # 分类频次从高到低：先修最主要的那个，别让一个偶发分类排在最前面
    ordered = sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))
    next_actions = [{"category": cat, "action": NEXT_ACTIONS.get(cat, NEXT_ACTIONS["unknown"])}
                    for cat, _ in ordered if cat in categories]
    if failed_steps and not next_actions:
        next_actions = [{"category": "unknown", "action": NEXT_ACTIONS["unknown"]}]
    # 一条步骤都没跑起来（设备连不上最典型）：failed_steps 为空 ⇒ 上面两条都不触发，
    # `next_actions` 会是空数组——agent 拿到"出错但没建议"只能自己猜。这是它最需要
    # 下一步动作的一类失败，按 env-failure 给（修环境而不是改用例）。
    if verdict == "error" and not next_actions:
        next_actions = [{"category": "env-failure",
                         "action": NEXT_ACTIONS["env-failure"]}]

    notes: list[str] = []
    if verdict == "pass" and payload.get("status") not in (None, "passed"):
        notes.append(f"退出码是 0 但 run.status={payload.get('status')}——产物与判定不一致，已按产物记")
    if any(f["false_red"] for f in failed_steps):
        notes.append("存在假红（分类 env-failure 但 App 实际在前台）：统计时单独计，别当环境噪声")
    if any(s.get("heal") for s in steps):
        notes.append("有自愈提案待确认：`mat heal list <产物目录>` 查看，原用例未被改动")
    if any(f.get("off_page") for f in failed_steps):
        notes.append("有步骤跑到了别的页（off_page）：先判断是 App 自己跳走还是用例前置没到位")

    from .verify_models import VerifyVerdict

    base = {
        "schema": SCHEMA,
        "verdict": verdict,
        "ok": verdict == "pass",
        "exit_code": int(exit_code),
        # 协议固定字段(2026-10-31 评审 P0-1):模型默认(queued=False/wait_ms=0),
        # 所有分支统一从模型 .model_dump() 出口——漏字段在生成时报错,不是消费方炸
        "queued": False,
        "wait_ms": 0,
        "case_id": payload.get("case_id") or "",
        "case_name": payload.get("case_name") or "",
        "status": payload.get("status"),
        "step_success_rate": payload.get("step_success_rate"),
        "duration_ms": payload.get("duration_ms"),
        "steps": {"total": len(steps),
                  "passed": len(steps) - len(failed_steps),
                  "failed": len(failed_steps)},
        "failed_categories": categories,
        "failed_steps": failed_steps,
        "next_actions": next_actions,
        "preconditions": payload.get("preconditions") or [],
        "artifacts": {
            "dir": str(artifacts),
            "run_json": str(artifacts / "run.json"),
            "report": str(report) if report else None,
        },
        "notes": notes,
        "verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return VerifyVerdict.model_validate(base).model_dump(by_alias=True)


def render_text(v: dict) -> str:
    """人读的一小段（≤8 行）。给 agent 的一律用 `--json`，这段是给顺手看一眼的人。"""
    steps = v.get("steps") or {}
    head = (f"VERIFY {v['verdict'].upper()}  exit={v['exit_code']}  "
            f"{v.get('case_name') or v.get('case_id')}  "
            f"步骤 {steps.get('passed', 0)}/{steps.get('total', 0)}")
    lines = [head]
    if v.get("failed_categories"):
        cats = "  ".join(f"{k}×{n}" for k, n in v["failed_categories"].items())
        lines.append(f"失败分类: {cats}")
    for item in (v.get("next_actions") or [])[:2]:
        lines.append(f"下一步[{item['category']}]: {item['action']}")
    for note in (v.get("notes") or [])[:2]:
        lines.append(f"注意: {note}")
    lines.append(f"产物: {v['artifacts']['run_json']}")
    return "\n".join(lines)


# -- 执行（CLI 与 MCP 共用，避免两条路各写一份） -----------------------------


#: 控制台默认端口（`mat console` 的默认值）；`MAT_CONSOLE_PORT` 可覆盖，
#: 探测失败/控制台没起都**静默跳过**——这条守卫不该成为新的失败源。
CONSOLE_PORT = 7420
#: 控制台里哪些任务会真的抢设备（跑用例/探索/装包）
_BUSY_KINDS = ("run", "explore", "install")


def _console_busy(serial: str | None) -> dict | None:
    """查控制台是否有正在占用设备的任务；有则返回该任务，否则 None。

    为什么需要它（2026-10-31）：`device_lock` 是**进程内**锁，控制台与
    `mat verify` 是两个进程——控制台正在跑套件时 agent 再发 verify，两边会同时
    初始化同一台设备的 u2/ATX 服务，产出"看着正常、实际无效"的结果。
    跨进程文件锁是后续项，但这里能用一次 HTTP 探测把最常见的撞车挡在门外。

    保守口径：任一类抢设备的任务在跑就算忙（单设备环境里几乎必然是同一台）；
    只有**双方 serial 都明确且不同**时才放行。
    """
    import json
    import os
    import urllib.request

    if os.environ.get("MAT_VERIFY_SKIP_CONSOLE_CHECK") == "1":
        return None
    port = int(os.environ.get("MAT_CONSOLE_PORT") or CONSOLE_PORT)
    url = f"http://127.0.0.1:{port}/api/jobs"
    headers = {}
    key = os.environ.get("MAT_CONSOLE_KEY")
    if key:
        # --share 模式 /api/* 全部要口令（X-MAT-Key 头）——不带它探测必 401,
        # 守卫在**最该防撞车的多人场景**形同虚设(2026-10-31 评审 P0-2)
        headers["X-MAT-Key"] = key
    try:
        # 显式禁用代理：本机回环不该走 http_proxy（开着代理探测必超时，实测踩过）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=0.4) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001 - 控制台没起/端口被占/格式变了：一律静默放行
        return None
    for job in data.get("jobs") or []:
        if _job_is_busy(job, serial):
            return job
    return None


def _job_is_busy(job: dict, serial: str | None) -> bool:
    """这条控制台任务是否占着**我们要用的那台**设备（判据抽出来便于单测）。

    保守优先：只有两类任务算"抢设备"（run/explore/install）且状态在跑；
    只有**双方 serial 都明确且不同**时才判为不冲突——单设备环境里
    serial 常常是 None，按"不同"处理会把守卫做成摆设。
    """
    if str(job.get("status") or "") not in ("running", "queued"):
        return False
    if str(job.get("kind") or "") not in _BUSY_KINDS:
        return False
    job_serial = job.get("serial")
    if serial and job_serial and str(job_serial) != str(serial):
        return False
    return True


# -- 装包（协议 v3 §3.1,仅 Android v1）----------------------------------------
#
# 纪律:① 装错包再跑用例是最坏结果 ⇒ 装完立即校验包名,验不过**卸载回退**;
# ② 安装本身要留审计行(_installs.jsonl)——谁在什么时候装了什么;
# ③ 路径安全:APK 只接受工作区内路径(工作区应开在被测 App 仓库根,见 v4 使用前提)。

INSTALL_KEEP_LINES = 5000
INSTALL_TTL_SECONDS = 30 * 86400


def _install_audit(root: Path, row: dict) -> None:
    """追加一行安装审计并滚动(5000 行/30 天,双限取先)。"""
    p = Path(root) / "artifacts" / "verify" / "_installs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    lines.append(json.dumps(row, ensure_ascii=False))
    p.write_text("\n".join(lines[-INSTALL_KEEP_LINES:]) + "\n", encoding="utf-8")


def _check_pkg_installed(adb, serial: str, pkg: str) -> bool:
    out = adb(serial, "shell", "pm", "path", pkg)
    return bool(out and "package:" in out)


def install_apk(root: Path, adb, serial: str, apk: Path, expected_pkg: str,
                reinstall: bool = False) -> dict:
    """安装 APK 并校验包名。返回 {ok, pkg, sha256, error?}。

    ⚠️ **参数不合法时抛 `ValueError`**（工作区外路径 / 非 `.apk` / 目录）——这类是
    调用方写错了参数、不是运行时失败，抛出来逼调用方处理。**调用方必须捕获并转成
    `install` 分类的 error 结论**（单条 `run_verify` 与批量 `run_subset` 都这么做）；
    之前这里写着"不抛异常"，正是批量路径漏捕获的来源。

    包名校验分两段：
    1. **装前**读 APK 清单里的 `package`（`apkmeta`，纯标准库），不符 ⇒ 直接返回、
       **不碰设备**——这是协议文字「不一致 ⇒ 不安装」的原意；
    2. 装前读不出（畸形包/非 AXML）才退回**装后** `pm path <expected>` 兜底，
       验不过即卸载（错误包不留存）。
    """
    import hashlib

    apk = Path(apk)
    resolved = apk.resolve()
    root_res = Path(root).resolve()
    # 用路径语义判断包含关系，**不能用字符串前缀**：
    # `/Users/x/proj-evil/a.apk` 以 `/Users/x/proj` 开头，str.startswith 会放行
    # （2026-11-02 复核）。`is_relative_to` 按路径分量比较，兄弟目录会被正确拒绝。
    if not resolved.is_relative_to(root_res):
        raise ValueError(
            f"APK 在工作区外({apk})——MAT 工作区应开在被测 App 仓库根:"
            "把 APK 拷进工作区,或在 App 仓库根重新 `mat init`")
    if not apk.is_file() or apk.suffix != ".apk":
        raise ValueError(f"只接受 .apk 文件:{apk}")
    sha = hashlib.sha256(apk.read_bytes()).hexdigest()
    row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "path": str(apk),
           "sha256": sha[:16], "serial": serial}
    # 装前校验包名（2026-10-31）：只靠装后 `pm path <期望包>` 判断，在「APK 实际包名
    # 与期望不同、且该包本来就装着」时必然失灵——覆盖安装不新增包名，前后 diff 什么也
    # 看不见，只能报"未知"（真机实测）。能装前判就**不碰设备**，与协议文字
    # 「不一致 ⇒ 不安装」一致；解析不了（畸形 APK）再走下面的装后校验兜底。
    from .apkmeta import read_package_name

    real_pkg = read_package_name(apk)
    if real_pkg and real_pkg != expected_pkg:
        row["result"] = f"mismatch(apk={real_pkg}): expected {expected_pkg}"
        _install_audit(root, row)
        return {"ok": False, "pkg": real_pkg, "sha256": sha,
                "error": (f"APK 实际包名是 {real_pkg}，与期望的 {expected_pkg} 不符——"
                          "**没有安装**。核对 --package 或用例的 target.package")}
    if reinstall:
        adb(serial, "uninstall", expected_pkg)
    out = adb(serial, "shell", "pm", "list", "packages", "-3")
    before = {line.split(":")[1] for line in (out or "").splitlines() if ":" in line}
    rc = adb(serial, "install", "-r", "-t", str(apk), timeout=600)
    if rc is None or "Success" not in rc:
        err = (rc or "adb install 失败").splitlines()[-1][:160]
        row["result"] = f"failed: {err}"
        _install_audit(root, row)
        return {"ok": False, "pkg": expected_pkg, "sha256": sha, "error": f"安装失败:{err}"}
    # 兜底（装前读不出包名时）：期望包必须可寻址；本次新增的第三方包记下来，验不过就卸
    if not _check_pkg_installed(adb, serial, expected_pkg):
        new_pkgs = []
        out = adb(serial, "shell", "pm", "list", "packages", "-3")
        for line in (out or "").splitlines():
            pkg_name = line.split(":")[1] if ":" in line else ""
            if pkg_name and pkg_name not in before:
                new_pkgs.append(pkg_name)
        for stray in new_pkgs:
            adb(serial, "uninstall", stray)
        row["result"] = f"mismatch: expected {expected_pkg}, got {new_pkgs or '?'}"
        _install_audit(root, row)
        detail = (f"实际装进去的是 {', '.join(new_pkgs)}，已卸载回退" if new_pkgs
                  else "APK 清单读不出包名，装后也没发现新增包，已回退")
        return {"ok": False, "pkg": new_pkgs[0] if new_pkgs else expected_pkg,
                "sha256": sha,
                "error": (f"装进来的不是期望的 {expected_pkg}（{detail}）——"
                          "确认 --package 或用例 target.package 后重试")}
    row["result"] = "ok"
    _install_audit(root, row)
    return {"ok": True, "pkg": expected_pkg, "sha256": sha}


def _install_error_verdict(case, art: Path, info: dict, note: str) -> dict:
    """装包阶段失败的统一出口：error + install 明细 + **install 分类的下一步**。

    为什么不靠 build_verdict 的通用兜底：那边会把无步骤的 error 归到 `env-failure`
    （"设备断开/App 不在前台/锁屏"）——而装包失败时用例一条都没跑，让 agent 去查环境
    是把排查方向带偏（2026-10-31 真机实测：`--package` 写错却被告知先修环境）。
    """
    verdict = build_verdict({"case_id": case.case_id, "case_name": case.name,
                             "status": "error", "steps": []}, EXIT_ERROR, art)
    verdict["install"] = info
    verdict["next_actions"] = [{"category": "install",
                                "action": NEXT_ACTIONS["install"]}]
    verdict["notes"].append(note)
    return verdict


def _persist_verdict(verdict: dict) -> None:
    """把判定落盘到产物目录（`verify.json`）——**给人回看 agent 当时看到了什么**。

    为什么必须落盘（2026-11-01）：判定只打到 stdout，一次输出后就没了；控制台想渲一张
    「验证结论」卡只能自己重算一遍（两套判定迟早分叉），人也无法复盘「agent 为什么判失败」。
    写盘失败**静默**：落盘是附加价值，不能反过来影响判定本身。
    """
    try:
        directory = (verdict.get("artifacts") or {}).get("dir")
        if not directory:
            return
        path = Path(directory) / "verify.json"
        path.write_text(json.dumps(verdict, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    except OSError:
        pass


def run_verify(root: Path, case_path: Path, *, serial: str | None = None,
               platform: str = "android", artifacts: Path | None = None,
               allow_unmet: bool = False, quality_gate: bool = True,
               timeout_s: float | None = 180, apk: Path | None = None,
               package: str | None = None, reinstall: bool = False,
               execute_timeout_s: float | None = None) -> tuple[int, dict]:
    """跑一条用例并返回 `(exit_code, verdict)`；判定同时落盘 `verify.json`。

    `timeout_s` 是**排队**上限（等设备锁）；`execute_timeout_s` 是**执行**上限
    （2026-11-02 复核）：此前批量预算只能事后扣减，一条卡死的用例会拖死整批。
    执行超时走 Runner 既有的 `cancel_event`——在**下一步开始前**安全退出，当前步
    证据保留，status="cancelled" ⇒ `incomplete`(4)，不能谎报成用例失败(2)。

    刻意复用 `cmd_run` 的**同一批**私有辅助（连设备、前置条件判据）：验证与执行必须是
    同一套语义，否则 agent 会看到「run 说环境错、verify 说定位失败」这种互相矛盾的结果。

    落盘放在这个薄壳里而不是每个 `return` 前面：函数有 8 个出口，逐个加必然漏。
    """
    code, verdict = _run_verify(root, case_path, serial=serial, platform=platform,
                                artifacts=artifacts, allow_unmet=allow_unmet,
                                quality_gate=quality_gate, timeout_s=timeout_s,
                                apk=apk, package=package, reinstall=reinstall,
                                execute_timeout_s=execute_timeout_s)
    _persist_verdict(verdict)
    return code, verdict


def _run_verify(root: Path, case_path: Path, *, serial: str | None = None,
                platform: str = "android", artifacts: Path | None = None,
                allow_unmet: bool = False, quality_gate: bool = True,
                timeout_s: float | None = 180, apk: Path | None = None,
                package: str | None = None, reinstall: bool = False,
                execute_timeout_s: float | None = None) -> tuple[int, dict]:
    """`run_verify` 的实现体（不落盘）；外部一律走 `run_verify`。"""
    from .__main__ import _blocked_preconditions, _connect_driver
    from .dsl import TestCase
    from .preconditions import refusal_message
    from .report import render_html
    from .runner import Runner
    from .web_api.runtime import device_lock

    case_path = Path(case_path)
    raw = json.loads(case_path.read_text(encoding="utf-8"))
    case = TestCase.model_validate(raw)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    art = Path(artifacts) if artifacts else Path(root) / "artifacts" / "verify" / f"{case_path.stem}-{stamp}"
    art.mkdir(parents=True, exist_ok=True)

    busy = _console_busy(serial)
    if busy is not None:
        verdict = build_verdict({"case_id": case.case_id, "case_name": case.name,
                                 "status": "skipped", "steps": []}, EXIT_BLOCKED, art)
        verdict["blocked_reason"] = "device_busy"
        verdict["next_actions"] = [{"category": "device-busy",
                                    "action": NEXT_ACTIONS["device-busy"]}]
        verdict["notes"].append(
            f"控制台任务执行中（{busy.get('kind')} · {str(busy.get('id'))[:8]}，"
            f"serial={busy.get('serial') or '默认设备'}）——等它跑完再 verify；"
            "两个进程同时抢同一台手机会产出无效结果。")
        return EXIT_BLOCKED, verdict

    install_info: dict | None = None
    if apk is not None:
        if platform != "android":
            return EXIT_ERROR, _install_error_verdict(
                case, art, {"ok": False, "error": "本版装包仅支持 Android"},
                "本版装包仅支持 Android(apk)——iOS/harmony 后续")
        expected = (package or (case.target or {}).get("package") or "").strip()
        if not expected:
            return EXIT_ERROR, _install_error_verdict(
                case, art, {"ok": False, "error": "无法确定期望包名"},
                "无法确定期望包名:用例没有 target.package,也没给 --package")
        from .adb import adb as _adb

        try:
            install_info = install_apk(root, _adb, serial, Path(apk), expected,
                                       reinstall=reinstall)
        except ValueError as exc:   # 路径/扩展名校验失败:可行动报错,exit=1
            return EXIT_ERROR, _install_error_verdict(
                case, art, {"ok": False, "error": str(exc)}, str(exc))
        if not install_info["ok"]:
            return EXIT_ERROR, _install_error_verdict(
                case, art, install_info, f"装包失败:{install_info['error']}")

    blocked = _blocked_preconditions(case, allow_unmet)
    if blocked:
        verdict = build_verdict({"case_id": case.case_id, "case_name": case.name,
                                 "status": "skipped", "steps": [],
                                 "preconditions": list(case.preconditions or [])},
                                EXIT_BLOCKED, art)
        verdict["blocked_reason"] = "precondition"
        verdict["next_actions"] = [{"category": "precondition",
                                    "action": NEXT_ACTIONS["precondition"]}]
        verdict["notes"].append(refusal_message(case.name or case.case_id, blocked))
        verdict["blocked_preconditions"] = blocked
        if install_info is not None:
            verdict["install"] = install_info   # 装包与拒跑分开报——别让 agent 以为白装
        return EXIT_BLOCKED, verdict

    # 设备锁（协议 v3 §3.3）：两段式 acquire——先试立即拿（queued=false 快路径）。timeout_s 是**排队等待上限**（不含用例执行——runner 无执行超时,文案与实现一致），
    # 拿不到再等剩余额度（queued=true）。connect 与 run **一起进锁**（v3 修订 4：
    # 连接本身会竞争同一台设备的 ATX 服务，锁只包 run 是无效防护）。
    # ⚠️ 超时 ⇒ 直接返回，**绝不连设备**——与控制台占用守卫同一纪律。
    lock = device_lock(serial)
    timeout_s = 180 if timeout_s is None else max(0.0, float(timeout_s))
    queued = lock.acquire(timeout=0) is False
    if queued:
        t0 = time.perf_counter()
        acquired = lock.acquire(timeout=timeout_s)
        wait_ms = round((time.perf_counter() - t0) * 1000)
    else:
        acquired = True
        wait_ms = 0
    if not acquired:
        verdict = build_verdict({"case_id": case.case_id, "case_name": case.name,
                                 "status": "skipped", "steps": []}, EXIT_ERROR, art)
        verdict["queued"] = True
        verdict["wait_ms"] = wait_ms
        verdict["notes"].append(
            f"排队等待超过 {timeout_s}s——**这是排队不是用例失败**，不要改用例；"
            "等占用方（控制台任务/另一个 verify）跑完再试。")
        return EXIT_ERROR, verdict

    try:
        drv = _connect_driver(serial, platform)
        if drv is None:
            verdict = build_verdict({"case_id": case.case_id, "case_name": case.name,
                                     "status": "error", "steps": []}, EXIT_ERROR, art)
            verdict["queued"] = queued
            verdict["wait_ms"] = wait_ms
            verdict["notes"].append("设备连接失败：先 `mat doctor` 确认设备在线与授权，再重跑")
            return EXIT_ERROR, verdict

        # 执行超时（2026-11-02）：复用 Runner 的 cancel_event（下一步开始前退出），
        # 不新造一套中断机制。Timer 是 daemon，run 结束/异常都会 cancel。
        runner = Runner(drv, art, quality_gate=quality_gate)
        cancel = None
        timer = None
        if execute_timeout_s is not None:
            cancel = threading.Event()
            timer = threading.Timer(max(0.0, float(execute_timeout_s)), cancel.set)
            timer.daemon = True
            timer.start()
        try:
            # 不在默认路径上传 `cancel_event=None`：它是可选参数，但测试/旧驱动的
            # Runner 替身可能没有这个形参——保持原调用签名（2026-11-02 实测踩中）。
            if cancel is not None:
                run = runner.run(case, cancel_event=cancel)
            else:
                run = runner.run(case)
        finally:
            if timer is not None:
                timer.cancel()
    finally:
        lock.release()
    report = render_html(run, art)
    if run.status == "passed":
        code = EXIT_PASS
    elif run.status == "cancelled":
        # 超时/调用方取消：当前步跑完后退出，证据保留 —— 这是"没跑完"，不是用例失败。
        code = EXIT_INCOMPLETE
    else:
        code = EXIT_FAIL
    verdict = build_verdict(run.model_dump(), code, art, report)
    if run.status == "cancelled":
        verdict["notes"].append(
            "执行被中止（超出 execute_timeout 或调用方取消）——已完成的步骤证据保留，"
            "不是用例失败")
    verdict["queued"] = queued
    verdict["wait_ms"] = wait_ms
    if install_info is not None:
        verdict["install"] = install_info
    return code, verdict


# -- 截图（2026-10-30，协议 v3 §3.2）-----------------------------------------
#
# 给 agent 的"看一眼现场"能力。三个纪律：
# 1. **返回图本身**，不只是路径——agent 读不了本地文件，只给路径等于没给；
# 2. **必须缩边**（默认 1280）：原图内联是几十万 token，默认行为不能有害；
# 3. **只读**：不点击、不改状态，失败也只返回 ok=false，不抛。

#: 内联图片的安全上限（最长边）。`MAT_VL_MAX_EDGE` 只能调得更小，参数优先。
SCREENSHOT_MAX_EDGE = 1280
#: `_screens/` 保留策略：最多 200 张、最多 7 天（取先到者）
SCREENSHOT_KEEP = 200
SCREENSHOT_TTL_SECONDS = 7 * 24 * 3600


def _prune_screens(shots_dir: Path, keep: int = SCREENSHOT_KEEP,
                   ttl_seconds: int = SCREENSHOT_TTL_SECONDS) -> int:
    """清理截图目录：删掉超期与超量的旧图，返回删除数量。

    为什么要清：agent 每验一次可能截好几张，不清理会把 `artifacts/` 堆成垃圾场——
    而这个目录还是控制台与报告共用的。清理失败**不影响**截图结果（best-effort）。
    """
    import time

    try:
        files = sorted((p for p in Path(shots_dir).glob("*.png")),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    deadline = time.time() - ttl_seconds
    removed = 0
    for i, p in enumerate(files):
        try:
            too_many = i >= keep
            too_old = p.stat().st_mtime < deadline
            if too_many or too_old:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def capture_screen(root: Path, *, serial: str | None = None, platform: str = "android",
                   max_edge: int = SCREENSHOT_MAX_EDGE) -> dict:
    """截当前屏幕：落盘原图 + 生成缩略图字节（JPEG）。

    返回 dict（不含 mcp 依赖，便于单测）：
    `{ok, path, width, height, thumb_width, thumb_height, foreground, taken_at,
       max_edge, jpeg}`；失败为 `{ok: False, error}`，**不抛异常**。
    """
    import io

    from PIL import Image

    from .__main__ import _connect_driver

    drv = _connect_driver(serial, platform)
    if drv is None:
        return {"ok": False, "error": "设备连接失败：先 `mat doctor` 确认设备在线与授权"}

    shots = Path(root) / "artifacts" / "verify" / "_screens"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    raw = shots / f"{stamp}.png"
    try:
        drv.screenshot(str(raw))
    except Exception as exc:  # noqa: BLE001 - 截图失败是业务失败，不抛
        return {"ok": False, "error": f"截图失败: {type(exc).__name__}: {exc}"}

    try:
        img = Image.open(raw)
        width, height = img.size
        edge = int(max_edge) if max_edge else SCREENSHOT_MAX_EDGE
        scale = min(1.0, edge / max(width, height))
        thumb = img.convert("RGB")
        if scale < 1.0:
            thumb = thumb.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                                 Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        thumb.save(buf, format="JPEG", quality=85, optimize=True)
    except Exception as exc:  # noqa: BLE001 - 缩略图失败同样只是"这次没图"
        return {"ok": False, "error": f"生成缩略图失败: {type(exc).__name__}: {exc}"}

    foreground = ""
    try:
        foreground = str((drv.foreground_app() or {}).get("package") or "")
    except Exception:  # noqa: BLE001 - 前台包名是诊断信息，取不到就空着
        pass

    _prune_screens(shots)
    try:
        rel = raw.relative_to(Path(root)).as_posix()
    except ValueError:
        rel = raw.as_posix()
    return {"ok": True, "path": rel, "width": width, "height": height,
            "thumb_width": thumb.width, "thumb_height": thumb.height,
            "foreground": foreground, "max_edge": edge,
            "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "jpeg": buf.getvalue()}


def build_screenshot_content(result: dict, include_image: bool = True) -> list:
    """把 `capture_screen` 的结果转成 MCP content 列表。

    形态固定为 `list`（1 个 TextContent + 可选 1 个 ImageContent）：调用方不必分支判断
    「这次返回的是 dict 还是 list」。`jpeg` 字节不进文本，避免把二进制塞进 JSON。
    """
    import base64

    from mcp.types import ImageContent, TextContent

    meta = {k: v for k, v in result.items() if k != "jpeg"}
    content: list = [TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))]
    if include_image and result.get("jpeg"):
        content.append(ImageContent(type="image",
                                    data=base64.b64encode(result["jpeg"]).decode("ascii"),
                                    mimeType="image/jpeg"))
    return content
