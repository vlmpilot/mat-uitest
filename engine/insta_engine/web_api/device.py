"""设备域：/api/devices、/api/screenshot（投屏）、/api/device/elements|key|scrcpy、
/api/ios/simulators|sim/boot（iOS 模拟器枚举与单活切换）。"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import UTC, datetime

from .. import ios_wda
from ..device import list_adb_devices
from ..ui_tree import parse_nodes
from .runtime import get_driver, invalidate_driver

#: 设备级操控动作白名单（F3）：拒绝一切自由文本，其余一律 400。
_KEY_ACTIONS = ("home", "back", "recents", "settings")


def api_devices() -> tuple[int, dict]:
    """GET /api/devices：adb 枚举天然只有 Android；逐项标注 platform 供上层按平台
    路由（评审 I4）。iOS 设备不可枚举（WDA 常驻 localhost:8100），可达性在首次连接时校验。
    """
    devices = [{**d, "platform": "android"} for d in list_adb_devices()]
    return 200, {"devices": devices}


def _simctl_devices() -> dict | None:
    """xcrun simctl 枚举可用模拟器（JSON）；xcrun 缺失/超时/输出异常返回 None。"""
    try:
        out = subprocess.run(
            ["xcrun", "simctl", "list", "-j", "devices", "available"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        ).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def api_ios_simulators() -> tuple[int, dict]:
    """GET /api/ios/simulators：枚举 iOS 模拟器（名称/udid/启动状态/系统版本）。

    支「设备投屏」页切换模拟器：booted 的可直接投屏（WDA 常驻 localhost:8100），
    未启动的可一键启动（POST /api/ios/sim/boot）。xcrun 不可用返回空列表不报错
    （未装 Xcode 的机器 iOS 功能本就不可用，doctor 有专门检查项提示）。
    """
    data = _simctl_devices()
    if data is None:
        return 200, {"simulators": []}
    sims: list[dict] = []
    for runtime, devs in (data.get("devices") or {}).items():
        # runtime 形如 com.apple.CoreSimulator.SimRuntime.iOS-18-2 → iOS 18.2
        version = (runtime.rsplit("iOS-", 1)[-1].replace("-", ".")
                   if "iOS-" in runtime else runtime.rsplit(".", 1)[-1])
        for d in devs:
            if not d.get("isAvailable", True):
                continue
            sims.append({"udid": d.get("udid", ""), "name": d.get("name", ""),
                         "state": "booted" if d.get("state") == "Booted" else "shutdown",
                         "os": f"iOS {version}"})
    # 排序（发起人 2026-10-12）：booted 优先（当前可投屏）→ iPhone 在前 → 系统版本
    # 降序 → 名称。iPhone/iPad/其他分三层而非纯字典序——设备多时 iPhone 应一眼可见。
    def _tier(s: dict) -> int:
        return 0 if "iPhone" in s["name"] else (1 if "iPad" in s["name"] else 2)

    def _ver(os_label: str) -> tuple[int, ...]:
        try:
            return tuple(int(p) for p in os_label.split()[-1].split("."))
        except ValueError:
            return (0,)

    sims.sort(key=lambda s: (s["state"] != "booted", _tier(s),
                             tuple(-v for v in _ver(s["os"])), s["name"]))
    return 200, {"simulators": sims}


def api_ios_sim_boot(body: dict) -> tuple[int, dict]:
    """POST /api/ios/sim/boot：启动指定模拟器并关闭其余已启动的（单活切换）。

    WDA 常驻 localhost:8100 服务的是「当前 booted」的模拟器——多开并存时投屏/执行
    指向不明，切换语义必须是 boot 目标 + shutdown 其余。udid 走 simctl 清单白名单校验。
    """
    udid = str(body.get("udid", "")).strip()
    data = _simctl_devices()
    if data is None:
        return 400, {"error": "xcrun simctl 不可用（需安装 Xcode 命令行工具）"}
    known = {d.get("udid"): d for devs in (data.get("devices") or {}).values()
             for d in devs if d.get("udid")}
    if udid not in known:
        return 400, {"error": f"未知模拟器 udid: {udid[:64]}"}
    # 已在启动状态时 boot 非零退出（current state: Booted）——幂等处理，不算失败
    subprocess.run(["xcrun", "simctl", "boot", udid], capture_output=True,
                   text=True, timeout=90)
    shut: list[str] = []
    for d in known.values():
        if d.get("state") == "Booted" and d.get("udid") != udid:
            subprocess.run(["xcrun", "simctl", "shutdown", d["udid"]],
                           capture_output=True, text=True, timeout=60)
            shut.append(d.get("name", ""))
    subprocess.run(["open", "-a", "Simulator"], capture_output=True, timeout=15)  # best-effort 显示窗口
    # boot 完顺手拉 WDA（发起人 2026-10-12：启动完成即正常投屏——此前 WDA 要去终端
    # 手动跑，模拟器开了也投不了屏）。allow_build=True：全新模拟器 WDA 未安装，
    # simctl launch 必然失败——首次启动必须走构建兜底（分钟级，前端「启动中」占位
    # 承载等待）；装过 WDA 的模拟器走快路径秒级返回。
    wda_started, wda_detail = ios_wda.start_on_booted(allow_build=True)
    return 200, {"ok": True, "booted": udid, "shutdown": shut,
                 "wda": {"started": wda_started, "detail": wda_detail}}


def api_screenshot(serial: str | None, width: int | None = None,
                   platform: str = "android") -> tuple[int, bytes, str]:
    """width 传入时等比缩放（缩略图场景下 2MB 全尺寸 PNG 过重）。"""
    try:
        driver = get_driver(serial, platform)
        image = driver.screenshot_pil()
    except Exception:
        invalidate_driver(serial, platform)  # 旧连接失效（设备重插/超时），下次重建
        raise
    if width:
        ratio = width / image.width
        image = image.resize((width, int(image.height * ratio)))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return 200, buffer.getvalue(), "image/png"


def api_screenshot_capture(root, host: str, body: dict) -> tuple[int, dict]:
    """人工截屏（迭代，发起人需求）：截当前设备画面，存工作区固定目录。

    目录：<工作区>/screenshots/（与测试产物 artifacts/ 分离——这是人拍的，不是用例证据）。
    文件名带时间戳与设备号，绝不覆盖；返回相对路径与可点 URL（screenshots 路由直出）。
    """
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    try:
        driver = get_driver(serial, platform)
        image = driver.screenshot_pil()
    except Exception as exc:
        invalidate_driver(serial, platform)
        return 400, {"error": f"截屏失败（设备不可用）: {type(exc).__name__}: {exc}"}
    out_dir = root / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    tag = (serial or platform).replace(":", "_").replace(".", "_")
    name = f"manual-{ts}-{tag}.png"
    out = out_dir / name
    image.save(out, format="PNG", optimize=True)
    return 200, {"ok": True, "file": str(out.relative_to(root)),
                 "url": f"/screenshots/{name}", "bytes": out.stat().st_size}


def api_elements(serial: str | None, platform: str = "android") -> tuple[int, dict]:
    """当前屏幕可交互元素（用例编辑器的"从屏幕选取控件"）。"""
    driver = get_driver(serial, platform)
    xml = driver.dump()
    current = {}
    try:
        current = driver.foreground_app()
    except Exception:
        pass
    package = current.get("package", "")
    elements = []
    for node in parse_nodes(xml):
        if package and not node.in_package(package):
            continue
        if not (node.clickable or node.text or node.desc):
            continue
        cx, cy = node.center or (None, None)
        elements.append({"text": node.text, "desc": node.desc, "rid": node.rid,
                         "cls": node.cls, "clickable": node.clickable,
                         "cx": cx, "cy": cy})
    return 200, {"elements": elements}


def api_device_key(body: dict) -> tuple[int, dict]:
    """设备级操控（F3）：白名单动作 × 在线 serial 校验。

    **不抢设备锁**（设计 §4.3）：手动干预恰是任务卡死时的救场通道；
    后果（可能干扰用例）由前端警示条讲清，选择权留给人。
    """
    action = str(body.get("action") or "")
    if action not in _KEY_ACTIONS:
        return 400, {"error": f"未知动作 {action!r}（支持: {'/'.join(_KEY_ACTIONS)}）"}
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    if platform == "android" and serial is not None and \
            serial not in {d["serial"] for d in list_adb_devices()}:
        return 400, {"error": f"设备 {serial} 不在线"}
    try:
        driver = get_driver(serial, platform)
        if action == "home":
            driver.home()
        elif action == "back":
            driver.back()
        elif action == "recents":
            driver.open_recents()
        else:
            driver.open_settings()
    except NotImplementedError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:  # 设备掉线/服务未起：如实 400，且失效缓存待下次重建
        invalidate_driver(serial, platform)
        return 400, {"error": f"设备不可用: {type(exc).__name__}: {exc}"}
    return 200, {"ok": True, "action": action}


def api_device_scrcpy(body: dict) -> tuple[int, dict]:
    """拉起 scrcpy 独立窗口（F3 方案 B）：真视频真流畅，匹配「认真调试」场景。

    安全边界：列表参数 Popen（不经 shell）；serial 必须命中在线列表；
    detached 启动——console 退出不带走投屏窗口。
    """
    import shutil
    import subprocess

    exe = shutil.which("scrcpy")
    if not exe:
        return 400, {"error": "未安装 scrcpy：brew install scrcpy（可选能力；浏览器内截图轮询不依赖它）"}
    serial = body.get("serial") or None
    if serial is not None and serial not in {d["serial"] for d in list_adb_devices()}:
        return 400, {"error": f"设备 {serial} 不在线"}
    args = [exe] if serial is None else [exe, "-s", serial]
    popen_kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(args, **popen_kwargs)
    return 200, {"ok": True, "serial": serial}
