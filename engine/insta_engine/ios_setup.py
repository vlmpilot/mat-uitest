"""iOS 真机引导（P2.1）：把踩过的坑变成一条可执行清单。

**为什么需要它**：真机上手要跨多道门（Xcode → 账号 → 团队 → 证书有效期 → 设备 → 解锁 →
WDA 起服务 → 已签名产物 → 证据源），
每道门的失败信息都不一样，其中三道**无法自动修复**（登录 Apple ID / 设备上信任证书 /
Xcode ⌘U 起 WDA）。本模块只做两件事：**如实检查 + 给出下一步动作**，不假装能一键搞定。

设计取舍：
- 复用 `doctor.Check`（与 `mat doctor` 同一套状态语义，配置台也能直接渲染）；
- 检查项**只读**：不登录、不信任、不构建——那些是用户动作；
- `--start` 才尝试 `start_on_device`（会构建，慢），默认只体检。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import ios_device
from .doctor import Check


def _xcode_version() -> str:
    if not shutil.which("xcodebuild"):
        return ""
    try:
        proc = subprocess.run(["xcodebuild", "-version"], capture_output=True,
                              text=True, timeout=20)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.splitlines()[0].replace("Xcode ", "").strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


#: 标准安装位置的兜底候选（模块级：单测可替换——测试机本身可能装着 Xcode）
FALLBACK_SIMULATOR_PATHS = [
    Path("/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app")]


def open_simulator_app(boot_first: bool = True) -> tuple[bool, str]:
    """打开模拟器（2026-10-26：确定性路径 + simctl boot 兜底）。

    两层现实：① Simulator.app 在 Xcode 包内，`open -a` 常找不到（LaunchServices 未
    注册包内位置）；② **精简安装的 Xcode 可能根本没有 Simulator.app**（用户机器实况：
    iOS platform/runtime 都装了、simctl 设备齐全，但包内无 GUI 应用）——此时直接
    `simctl boot` 一台可用模拟器，WDA/投屏不依赖 Simulator 窗口。

    ``boot_first``：GUI 打不开时是否直接 boot 一台（doctor 的「启动」入口 True；
    已 boot 指定设备的调用方传 False，只求开窗口、别多开机器）。
    """
    candidates: list[Path] = []
    try:
        proc = subprocess.run(["xcode-select", "-p"], capture_output=True,
                              text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            candidates.append(Path(proc.stdout.strip()) / "Applications" / "Simulator.app")
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.extend(FALLBACK_SIMULATOR_PATHS)
    for cand in candidates:
        if cand.exists():
            proc = subprocess.run(["open", str(cand)], capture_output=True, timeout=15)
            if proc.returncode == 0:
                return True, ""
    # 回退旧路径（LaunchServices 注册过的机器仍可用）
    proc = subprocess.run(["open", "-a", "Simulator"], capture_output=True, timeout=15)
    if proc.returncode == 0:
        return True, ""
    # GUI 应用不存在：直接 boot 一台可用模拟器（headless 即可跑 WDA/投屏）
    if boot_first:
        try:
            proc = subprocess.run(["xcrun", "simctl", "list", "devices", "available"],
                                  capture_output=True, text=True, timeout=15)
            devices = proc.stdout or ""
            if "(Booted)" in devices:
                return True, "已有启动中的模拟器（这台 Xcode 未包含 Simulator 窗口应用，不影响 WDA 与投屏）"
            m = re.search(r"\(([\w-]{36})\) \(Shutdown\)", devices)
            if m:
                udid = m.group(1)
                name = re.search(rf"^(.+?) \({udid}\)", devices, re.M)
                boot = subprocess.run(["xcrun", "simctl", "boot", udid],
                                      capture_output=True, text=True, timeout=60)
                if boot.returncode == 0:
                    return True, (f"已直接启动模拟器 {name.group(1).strip() if name else udid}"
                                  "（这台 Xcode 未包含 Simulator 窗口应用，不影响 WDA 与投屏）")
        except (OSError, subprocess.SubprocessError):
            pass
    # 分两种缺法给指引：Xcode 在但没装模拟器组件（可下载补齐）vs 没有 Xcode——
    # 以**开发者目录真实存在**为准（xcode-select 输出可能指向已卸载的旧位置）
    has_xcode = any(c.parent.parent.exists() for c in candidates if str(c))
    if has_xcode:
        return False, ("Xcode 已安装但缺少模拟器组件——在 Xcode → Settings → Components "
                       "下载 iOS platform，或终端运行 `xcodebuild -downloadPlatform iOS`；"
                       "也可以改用 USB 真机")
    return False, ("未找到 Simulator.app——请先安装完整 Xcode.app"
                   "（App Store 或 developer.apple.com），或改用 USB 真机")


def _signed_runner_exists() -> bool:
    """Xcode 是否已产出**已签名**的真机 runner（免账号启动的前提）。

    有它 ⇒ `start_on_device` 走复用路径，不必每次让用户按 ⌘U（2026-10-18 实测）。
    探测失败按"没有"处理——它只影响提示，不影响执行。
    """
    try:
        from .ios_wda import find_signed_device_runner

        return find_signed_device_runner() is not None
    except Exception:  # noqa: BLE001 —— 探测失败不阻断体检
        return False


def _wda_ready(udid: str) -> bool:
    # 单一实现（2026-10-22）：doctor 的 WDA 服务行与环境体检共用同一判据
    from .ios_wda import device_wda_ready

    return device_wda_ready(udid)


def _device_locked(udid: str) -> bool | None:
    """设备是否锁屏。`None` = 查不到（未装 wda / 连不上），调用方跳过该项。"""
    try:
        import wda  # 延迟导入：没装 [ios] extra 也能跑体检
    except ImportError:
        return None
    try:
        return bool(wda.USBClient(udid=udid).locked())
    except Exception:  # noqa: BLE001 —— 查不到就不下结论
        return None


def _evidence_available() -> bool:
    """失败步 syslog 证据源是否可用（pymobiledevice3 装没装）。

    为什么要专门查它：`[ios]` extra 的历史版本没声明这个依赖，装了 extra 的
    用户也会**静默没有失败证据**——"没有证据源" ≠ "没有日志"，必须显式暴露。
    """
    try:
        from .ios_syslog import SyslogBuffer

        return SyslogBuffer.available()
    except Exception:  # noqa: BLE001 —— 体检语义：探不到就按未装处理
        return False


def cert_check() -> Check | None:
    """证书有效期（到期时刻 + 分级）。无 profile 时返回 None（调用方跳过）。"""
    exp = ios_device.profile_expiry()
    if not exp:
        return None
    left = exp["days_left"]
    expires = exp.get("expires")
    when = (expires.astimezone().strftime("%m-%d %H:%M")
            if hasattr(expires, "astimezone") else "到期时间未知")
    if left < 0:
        return Check(
            id="cert-expiry", label=f"证书有效期: 已过期 {abs(left)} 天（{when} 到期）",
            ok=False, status="fail",
            hint="证书已过期：先用数据线（USB）连接 iPhone 并解锁再续期；续期成功后重启 WDA 才生效。"
                 "不行就在 Xcode 里对 WebDriverAgentRunner 按一次 ⌘U（免费证书 7 天）",
            fix={"action": "renew-cert"},
        )
    if left <= 3:
        # 有效期内**不给续期按钮**（2026-10-22 发起人）：Apple 只在必要时签发新
        # profile，有效期内点续期必然「构建完成但证书没有变化」——按钮只会制造一次
        # 假失败。等过期后转 fail（红）+ 给按钮，那时才真正能续。
        return Check(
            id="cert-expiry", label=f"证书有效期: 剩 {left} 天（{when} 到期）",
            ok=True, status="warn",
            hint="即将过期：到期后点「续期证书」重新签名（有效期内 Apple 不会签发新 profile）；"
                 "也可在 Xcode 里对 WebDriverAgentRunner 按一次 ⌘U（免费证书 7 天）",
        )
    return Check(id="cert-expiry", label=f"证书有效期: 剩 {left} 天（{when} 到期）", ok=True)


def lock_check(udid: str) -> Check | None:
    """设备是否锁屏。查不到（Wi-Fi / 未装 wda）时返回 None——**不凭空报红**。"""
    locked = _device_locked(udid)
    if locked is None:
        return None
    return Check(
        id="unlocked", label="设备解锁: 锁屏中" if locked else "设备解锁: 已解锁",
        ok=not locked, status="ok" if not locked else "fail",
        hint="" if not locked else
        "解锁 iPhone，并把「设置 → 显示与亮度 → 自动锁定」设为「永不」"
        "（有锁屏密码时 WDA 无法代你解锁；跑测期间也别按侧键锁屏）",
    )


def signed_runner_exists() -> bool:
    """Xcode 是否已产出**已签名**的真机 runner（免账号启动的前提）。"""
    try:
        from .ios_wda import find_signed_device_runner

        return find_signed_device_runner() is not None
    except Exception:  # noqa: BLE001 —— 探测失败不阻断体检
        return False


def signed_runner_check(signed: bool, wda_ok: bool) -> Check:
    """已签名产物：有它就能免账号自动起 WDA；WDA 已就绪时不算失败项。"""
    return Check(
        id="signed-runner",
        label=("已签名产物: 有（可免账号自动启动）" if signed else "已签名产物: 无"),
        ok=bool(signed) or wda_ok,
        status="ok" if (signed or wda_ok) else "warn",
        hint="" if (signed or wda_ok) else
        "在 Xcode 里对 WebDriverAgentRunner 按一次 ⌘U（生成签名产物后 mat 可自动起服务，"
        "不必每次都按）",
    )


def checks(serial: str | None = None) -> list[Check]:
    """逐项体检。顺序=用户实际要走的顺序，**第一项失败就是下一步动作**。

    注意：条数会随设备状态变化（如"设备解锁"查不到时整条跳过）——**别按固定条数断言**，
    测试里要显式打桩 `_device_locked`。
    """
    out: list[Check] = []

    version = _xcode_version()
    out.append(Check(
        id="xcode", label=f"Xcode: {version or '未找到'}", ok=bool(version),
        hint="" if version else
        "安装 Xcode（App Store）后执行 sudo xcode-select --switch /Applications/Xcode.app",
    ))

    # 先探"已签名产物"：**有它就不需要账号**（复用路径），这决定了下面几项的严重程度。
    signed = signed_runner_exists()

    # ⚠️ 必须读磁盘 plist：cfprefsd 缓存会显示"有账号"，但 xcodebuild 读不到（2026-10-17 实测）
    accounts = ios_device.xcode_account_count()
    if accounts:
        out.append(Check(id="account", label=f"Xcode 账号: {accounts} 个", ok=True))
    elif signed:
        # 有已签名产物时账号**不是阻塞项**（复用路径不用账号）——不能把它排成"下一步"，
        # 否则用户会去登录一个根本不需要的账号（2026-10-20 实测：账号空着也能跑 15×3）。
        out.append(Check(
            id="account", label="Xcode 账号: 未登录（可跳过——已有已签名产物）",
            ok=True, status="warn",
            hint="只有需要**重新构建** WDA 时才要登录账号；复用已签名产物不需要",
        ))
    else:
        out.append(Check(
            id="account", label="Xcode 账号: 未登录", ok=False,
            hint="Xcode → Settings → Accounts → + → Apple ID 登录（需 2FA）；"
                 "登录后退出 Xcode 让偏好落盘",
        ))

    team = ios_device.team_id()
    out.append(Check(
        id="team", label=f"签名团队: {team or '未找到'}", ok=bool(team), hint="" if team else
        "没有 Apple Development 证书：Xcode 登录账号后会自动创建（钥匙串里应有对应身份）",
    ))

    # 证书有效期：免费个人证书只有 7 天，过期后真机直接跑不动，且报错与"没签名"混在一起。
    cert = cert_check()
    if cert:
        out.append(cert)

    dev, why = ios_device.pick_device(serial)
    if dev is None:
        out.append(Check(id="device", label="真机: 未发现", ok=False, hint=why))
    else:
        usb = dev.get("transport") == "wired"
        mode = dev.get("developer_mode")
        ok = usb and bool(mode)
        hints = []
        if not usb:
            hints.append("用数据线连接（Wi-Fi 不在 V1 支持范围：USBClient 只认 USB）")
        if not mode:
            hints.append("设备上开「设置 → 隐私与安全性 → 开发者模式」")
        out.append(Check(
            id="device", label=f"真机: {ios_device.describe(dev)}", ok=ok,
            hint="；".join(hints),
        ))

    # 锁屏是**运行期**头号阻塞（实测：跑到第 5 条设备自动锁上，之后全红且报错像"没签名"）。
    # 放在 WDA 之前：锁屏时 WDA 必然"未就绪"，先看这条才不会被误导去重装 WDA。
    if dev is not None:
        lock = lock_check(dev["udid"])
        if lock is not None:
            out.append(lock)

    wda_ok = False
    if dev is None:
        out.append(Check(id="wda", label="WDA 服务: 未检查（无设备）", ok=False,
                         hint="先解决上一项"))
    else:
        wda_ok = _wda_ready(dev["udid"])
        if wda_ok:
            out.append(Check(id="wda", label="WDA 服务: 就绪", ok=True))
        else:
            # 路径必须跟着 WDA_SRC_DIR 走（2026-10-19 起带版本号）——写死旧路径会把
            # 用户指向一个不存在的目录。
            from .ios_wda import WDA_SRC_DIR

            if signed:
                hint = ("运行 `mat ios-setup --start` 自动拉起（已有已签名产物，"
                        "不需要账号）")
            else:
                hint = (f"在 Xcode 打开 {WDA_SRC_DIR}，scheme 选 WebDriverAgentRunner、"
                        "目标选你的 iPhone，按 ⌘U；若报 “certificate is not trusted”，"
                        "去 设置 → 通用 → VPN 与设备管理 → 开发者 App → 信任")
            out.append(Check(id="wda", label="WDA 服务: 未就绪", ok=False, hint=hint))

    # 已签名产物：**免账号启动的前提**（有它 mat 就能自己起 WDA，不必每次 ⌘U）。
    # 已经就绪时不把它算作失败项——用户此刻并不需要它。
    if dev is not None:
        out.append(signed_runner_check(signed, wda_ok))

    # 证据源（2026-10-19）：失败步的系统日志靠 pymobiledevice3 的 syslog（usbmux）。
    # 未装**不算失败**（执行不受影响），但必须显式 warn——此前它在依赖里根本没声明，
    # 用户装了 [ios] extra 也静默没有失败证据。
    if dev is not None:
        has_ev = _evidence_available()
        out.append(Check(
            id="evidence",
            label=("失败证据源 (syslog): 已就绪" if has_ev
                   else "失败证据源 (syslog): 未安装 pymobiledevice3"),
            ok=True, status="ok" if has_ev else "warn",
            hint="" if has_ev else
            "pip install 'mat-uitest[ios]'（含 pymobiledevice3）——失败步骤才能留下系统日志"
            "证据；不装不影响执行",
        ))

    return out


def render(items: list[Check]) -> str:
    """人读输出：逐项 ✅/❌ + 提示，末尾给「下一步」。"""
    lines = ["iOS 真机体检（按顺序解决）", ""]
    for item in items:
        mark = "✅" if item.ok else "❌"
        lines.append(f"  {mark} {item.label}")
        if not item.ok and item.hint:
            lines.append(f"       → {item.hint}")
    failed = [i for i in items if not i.ok]
    lines.append("")
    if not failed:
        lines.append("全部就绪：可以 mat run --platform ios --serial <udid> 跑用例了。")
        lines.append("⚠️ 执行期间保持设备解锁，并把「设置 → 显示与亮度 → 自动锁定」设为「永不」。")
    else:
        lines.append(f"下一步：{failed[0].hint or failed[0].label}")
    return "\n".join(lines)


def run(serial: str | None = None, start: bool = False, as_json: bool = False,
        renew: bool = False, force: bool = False) -> int:
    """体检；`start=True` 拉起 WDA（会构建，慢）；`renew=True` 先尝试续期证书。

    续期 = 重新签名一次（免费个人证书没有单独的续期 API）。能否成功取决于
    xcodebuild 能不能用账号——用不了就只能回 Xcode 按一次 ⌘U，这一点代码绕不过。
    """
    items = checks(serial)
    started = None
    renewed = None
    # 续期/启动都只认 USB（2026-10-22）：Wi-Fi 设备能配对但 WDA 只认 USB，试了也白试
    usb = [d for d in ios_device.list_devices() if d.get("transport") == "wired"]
    if renew:
        from .ios_wda import renew_device_profile

        dev, _why = ios_device.pick_device(serial, devices=usb)
        renewed = (False, "需要先用数据线（USB）连接 iPhone 并解锁；"
                          "续期成功后需重启 WDA 才生效") if dev is None \
            else renew_device_profile(dev["udid"], force=force)
        items = checks(serial)
    if start:
        dev, why = ios_device.pick_device(serial, devices=usb)
        if dev is None:
            started = (False, why or "没有 USB 连接的 iOS 真机——请用数据线连接并解锁")
        else:
            from .ios_wda import start_on_device

            started = start_on_device(dev["udid"])
            # 拉起后重跑一次体检，反映真实状态
            items = checks(serial)

    if as_json:
        print(json.dumps({"checks": [c.as_dict() for c in items],
                          "started": started}, ensure_ascii=False, indent=1))
    else:
        print(render(items))
        if renewed is not None:
            ok, detail = renewed
            print(f"\n续期证书：{'成功' if ok else '失败'} —— {detail}")
        if started is not None:
            ok, detail = started
            print(f"\n拉起 WDA：{'成功' if ok else '失败'} —— {detail}")
    return 0 if all(c.ok for c in items) else 1
