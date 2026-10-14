"""WDA 一键启动（iOS 模拟器 MVP，发起人 2026-10-12：概览红点只有 tips 没有入口）。

两级启动策略：
- **快路径**：装过 WDA 的模拟器上 `xcrun simctl launch <udid> <bundle>` 免构建拉起
  （首次 xcodebuild 会把 runner 装进模拟器，此后 simctl launch 即可），服务常驻
  localhost:8100；
- **构建兜底**（发起人 2026-10-12 实测：全新模拟器上 simctl launch 报 "The request
  to open ... failed"——WDA 从未装过，光会 launch 不够）：自动下载 WDA 源码
  （codeload tarball，GitHub 直连不通时仍可用）→ `xcodebuild
  build-for-testing` 构建 → `test-without-building` 分离进程拉起（它就是 WDA 服务
  本体，必须常驻——scrcpy 同款 detached 纪律，console 退出不带走）。
产物缓存在 ~/.mat/wda/（源码 + DerivedData），二次启动走快路径秒级就绪。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

#: WDA runner 的 bundle id（appium/WebDriverAgent 源码构建产物，模拟器与真机同 id）
WDA_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctruntime"
WDA_STATUS_URL = "http://localhost:8100/status"

#: WDA 源码与构建产物缓存（~/.mat 与 license/usage 同根；随版本升级可整目录删除重建）
WDA_SRC_DIR = Path.home() / ".mat" / "wda" / "WebDriverAgent"
WDA_TARBALL_URL = "https://codeload.github.com/appium/WebDriverAgent/tar.gz/refs/heads/master"

#: simctl 输出行里的 udid（36 位 UUID 形态）
_UDID_RE = re.compile(r"\(([0-9A-Fa-f-]{36})\)")

_BUILD_HINT = ("WDA 可能未装进该模拟器——首次需构建安装（复现命令见仓库 ："
               "xcodebuild build-for-testing + test-without-building）")


def _run(cmd: list[str], timeout_s: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)


def booted_udid() -> str | None:
    """当前 booted 模拟器的 udid（取第一台）；simctl 不可用/无 booted 返回 None。"""
    try:
        out = _run(["xcrun", "simctl", "list", "devices", "booted"], 10).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "(Booted)" not in line:
            continue
        m = _UDID_RE.search(line)
        if m:
            return m.group(1)
    return None


def _poll_status(seconds: int) -> bool:
    """轮询 WDA /status 就绪（首启初始化要几秒到几十秒）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(WDA_STATUS_URL, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 —— 轮询语义：失败即未就绪，继续等
            pass
        time.sleep(1)
    return False


def _last_error_line(proc: subprocess.CompletedProcess) -> str:
    lines = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
    return lines[-1][:300] if lines else f"退出码 {proc.returncode}"


def _ensure_wda_source() -> None:
    """WDA 源码就位（已存在跳过）：codeload tarball 下载 + 解压 + 归位。"""
    if (WDA_SRC_DIR / "WebDriverAgent.xcodeproj").exists():
        return
    base = WDA_SRC_DIR.parent
    base.mkdir(parents=True, exist_ok=True)
    tgz = base / "wda.tar.gz"
    subprocess.run(["curl", "-sL", "-o", str(tgz), WDA_TARBALL_URL],
                   check=True, timeout=180)
    subprocess.run(["tar", "-xzf", str(tgz), "-C", str(base)], check=True, timeout=120)
    extracted = next(p for p in base.glob("WebDriverAgent-*") if p.is_dir())
    extracted.rename(WDA_SRC_DIR)


def _build_wda(udid: str) -> Path:
    """build-for-testing（产物进 WDA_SRC_DIR/dd，不污染全局 DerivedData），返回 xctestrun。"""
    dd = WDA_SRC_DIR / "dd"
    proc = subprocess.run(
        ["xcodebuild", "-project", str(WDA_SRC_DIR / "WebDriverAgent.xcodeproj"),
         "-scheme", "WebDriverAgentRunner",
         "-destination", f"platform=iOS Simulator,id={udid}",
         "-derivedDataPath", str(dd), "build-for-testing"],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"xcodebuild 构建失败：{_last_error_line(proc)}")
    xruns = sorted((dd / "Build" / "Products").glob("*.xctestrun"))
    if not xruns:
        raise RuntimeError("构建完成但未找到 .xctestrun 产物")
    return xruns[0]


def _start_wda_service(udid: str, xctestrun: Path) -> None:
    """test-without-building 分离进程拉起——这就是 WDA 服务本体，必须常驻。"""
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    subprocess.Popen(["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                      "-destination", f"platform=iOS Simulator,id={udid}"], **kwargs)


def start_on_booted(wait_seconds: int = 40, allow_build: bool = True) -> tuple[bool, str]:
    """在当前 booted 模拟器上拉起 WDA。返回 (是否就绪, 给人看的详情)。

    - wait_seconds：快路径（simctl launch）的重试窗口——冷启动中的模拟器可能暂时失败；
    - allow_build：launch 明确「未安装」时是否走自动构建兜底。两个调用方现在都传
      True（发起人 2026-10-12：投屏页首次启动模拟器也必须自动装好 WDA，否则模拟器
      开了仍投不了屏）——装过 WDA 的模拟器走快路径秒级；全新模拟器首次构建分钟级，
      由前端「启动中」占位承载等待。保留开关供测试与未来快路径场景使用。
    """
    udid = booted_udid()
    if udid is None:
        return False, "没有已启动的模拟器——先启动模拟器再拉 WDA"
    deadline = time.monotonic() + wait_seconds
    last_err = ""
    launched = False
    while True:  # 快路径：装过 WDA 的模拟器免构建
        try:
            proc = _run(["xcrun", "simctl", "launch", udid, WDA_BUNDLE_ID], 20)
            if proc.returncode == 0:
                launched = True
                break
            last_err = _last_error_line(proc)
            if "not installed" in last_err.lower():
                break  # 未安装，重试无意义——交给构建兜底
        except (OSError, subprocess.SubprocessError) as exc:
            last_err = f"simctl 执行失败: {exc}"
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    if launched:
        return (True, "WDA 已就绪 (localhost:8100)") if _poll_status(15) else (
            False, "WDA 进程已拉起但 localhost:8100 未就绪（稍候重试投屏；仍不通）")
    if not allow_build:
        return False, f"WDA 启动失败：{last_err}；{_BUILD_HINT}"
    # 构建兜底：下载源码 → 构建 → 分离进程拉起（装好后下次启动走快路径）
    try:
        _ensure_wda_source()
        xctestrun = _build_wda(udid)
        _start_wda_service(udid, xctestrun)
    except (OSError, subprocess.SubprocessError, RuntimeError, StopIteration) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
        return False, f"WDA 自动构建失败：{detail}（手动复现）"
    return (True, "WDA 已就绪 (localhost:8100)——首次构建安装完成，下次启动秒级就绪") \
        if _poll_status(90) else (
        False, "WDA 已构建拉起但 localhost:8100 未就绪——稍候重试投屏，仍不通")
