"""iOS 真机：设备发现 / 签名团队 / 装卸载（P2，薄包装）。

**为什么是薄包装**：`devicectl`（Xcode 15+ 自带）已经把「列设备 / 装 App / 卸 App / 拉进程」
做完了，自己再解 usbmux 协议只会多一份会坏的东西。本模块只做三件事：
① 把 `devicectl` 的 JSON 变成引擎认识的字典；② 从钥匙串读签名团队；③ 拼命令。

**契约（doctor / ios-setup 都依赖）**：
- 所有函数**不抛**（除显式 `raise_on_error`）：环境坏掉时返回 `[]` / `None` + 原因，
  让上层渲染成"可行动提示"，而不是让用户看 traceback。
- 判据只用**机读字段**（`--json-output` 的 JSON），不解析 `devicectl` 的人类可读输出
  （那玩意儿随 Xcode 版本变）。

⚠️ **`tunnelState=disconnected` 不是"不可达"**（2026-10-17 实测）：`devicectl` 的列表快照
里隧道是**空闲态**，真正需要时会按需建立（命令输出 `Acquired tunnel connection to device`）。
判"能不能用"要看 `device info apps` 这类**需要隧道的操作**是否成功，不能看列表字段。
"""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

#: devicectl 单次调用超时（列设备很快；装 App 由调用方给更长的值）
_TIMEOUT = 20

#: 团队 id 形态：Apple 的 10 位大写字母数字
_TEAM_RE = re.compile(r"\b([A-Z0-9]{10})\b")


def _run(cmd: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def parse_devices(payload: dict) -> list[dict]:
    """`devicectl list devices --json-output` 的 JSON → 引擎字典列表（**纯函数**）。

    实测结构（Xcode 26.5）：`result.devices[]`，关键字段全在 `hardwareProperties` /
    `deviceProperties` / `connectionProperties` 三个子字典里；**顶层 `identifier` 不是 udid**
    （那是 coredevice id，`-destination id=` 用它会连不上）。
    """
    out: list[dict] = []
    for dev in ((payload.get("result") or {}).get("devices") or []):
        if not isinstance(dev, dict):
            continue
        hw = dev.get("hardwareProperties") or {}
        dp = dev.get("deviceProperties") or {}
        cp = dev.get("connectionProperties") or {}
        udid = (hw.get("udid") or "").strip()
        if not udid:
            continue
        out.append({
            "udid": udid,
            "name": (dp.get("name") or hw.get("marketingName") or "").strip(),
            "model": (hw.get("marketingName") or "").strip(),
            "os_version": str(dp.get("osVersionNumber") or "").strip(),
            "transport": (cp.get("transportType") or "").strip(),   # wired / localNetwork
            "tunnel": (cp.get("tunnelState") or "").strip(),        # 空闲态，不作可达判据
            "paired": (cp.get("pairingState") or "").strip() == "paired",
            "developer_mode": (dp.get("developerModeStatus") or "").strip() == "enabled",
        })
    return out


def list_devices(timeout: int = _TIMEOUT) -> list[dict]:
    """列出已配对真机（USB / Wi-Fi 都列）。失败返回 `[]`——调用方负责渲染提示。"""
    # 用 TemporaryDirectory（此前 mkdtemp + 只 unlink 文件 ⇒ 每次调用漏一个目录，
    # 而本函数在 doctor / ios-setup / 每次 --platform ios 运行都会调）
    with tempfile.TemporaryDirectory(prefix="mat-devices-") as tmpdir:
        tmp = Path(tmpdir) / "devices.json"
        try:
            _run(["xcrun", "devicectl", "list", "devices", "--json-output", str(tmp)], timeout)
            if not tmp.exists():
                return []
            return parse_devices(json.loads(tmp.read_text(encoding="utf-8")))
        except (OSError, subprocess.SubprocessError, ValueError):
            return []


def pick_device(serial: str | None = None, devices: list[dict] | None = None) -> tuple[dict | None, str]:
    """选一台设备：显式 serial 优先，否则**唯一**一台。返回 (设备, 原因)。

    多台时**不猜**（同 adb 的 `--serial` 纪律）：猜错设备的代价是"在别人的机器上跑用例"。
    """
    devices = list_devices() if devices is None else devices
    if not devices:
        return None, "没有已配对的 iOS 真机——用数据线连接 iPhone 并解锁"
    if serial:
        for dev in devices:
            if serial in (dev["udid"], dev["name"]):
                return dev, ""
        return None, f"找不到设备 {serial!r}（当前：{', '.join(d['name'] or d['udid'] for d in devices)}）"
    if len(devices) > 1:
        return None, ("检测到多台 iOS 真机，请用 --serial 指定其中一台："
                      + ", ".join(d["udid"] for d in devices))
    return devices[0], ""


#: 真机 udid 形态（同 wda_driver._DEVICE_UDID_RE，此处复制是为了让本模块在没装
#: facebook-wda 的环境也能独立工作——doctor/ios-setup 的契约是不依赖 wda）
_DEVICE_UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}$")
#: 模拟器 udid 形态：36 位 UUID（8-4-4-4-12）
_SIM_UDID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def resolve_ios_serial(serial: str | None, devices: list[dict] | None = None) -> str | None:
    """`--platform ios` 的 serial 统一解析（2026-10-19，审查 R9/R10 修复）。

    为什么需要它：WDA 客户端按 serial **形态**选路（真机 udid → usbmux，其余 →
    localhost）。传设备名 / 旧 40 位 UDID / 什么都传对不上时，WdaDriver 会把 serial
    当模拟器去连 localhost:8100——报错和真实原因（标识没解析对）完全无关，用户看不懂。

    规则（诚实、不猜设备）：
    - `None`/空/`"simulator"`/`"booted"`：自动选中**唯一一台 USB 真机**（用户插了线，
      缺省就该打真机；多台 USB 真机时报错要求指定，不猜）；没有 USB 真机则原样返回
      （连模拟器，行为与旧版一致）；
    - 8+16 hex（真机 udid）/ 36 位 UUID（模拟器 udid）：原样透传；
    - 设备名/营销名：映射为 udid——映射到 Wi-Fi 设备时报错（暂仅支持 USB，早报比
      usbmux 连接超时好）；
    - 其它（含旧 40 位 UDID）：报错并列出当前设备，提示用 `mat ios-setup` 查 udid。

    抛 `RuntimeError`（可行动提示在 message 里），由调用方渲染；`devices` 可注入便于单测。
    """
    if serial in (None, "", "simulator", "booted"):
        devs = list_devices() if devices is None else devices
        wired = [d for d in devs if d.get("transport") == "wired"]
        if len(wired) == 1:
            return wired[0]["udid"]
        if len(wired) > 1:
            raise RuntimeError(
                "检测到多台 USB 真机，请用 --serial 指定其中一台："
                + ", ".join(d["udid"] for d in wired))
        return serial  # 没有 USB 真机 ⇒ 模拟器（原行为）
    serial = str(serial).strip()
    if _DEVICE_UDID_RE.match(serial):
        # 显式 udid 也要校验 USB：否则 Wi-Fi 设备会一路走到 usbmux，报出
        # "'NoneType' object has no attribute 'make_http_connection'" 这种看不懂的错误。
        devs = list_devices() if devices is None else devices
        for d in devs:
            if d.get("udid") == serial:
                if d.get("transport") != "wired":
                    raise RuntimeError(
                        f"设备「{ios_like_name(d)}」当前经 Wi-Fi 连接——V1 仅支持 USB："
                        "请插数据线后重试（`mat ios-setup` 可查连接方式）")
                break
        return serial
    if _SIM_UDID_RE.match(serial):
        return serial
    devs = list_devices() if devices is None else devices
    for d in devs:
        if serial in (d.get("udid"), d.get("name"), d.get("model")):
            if d.get("transport") != "wired":
                raise RuntimeError(
                    f"设备「{ios_like_name(d)}」当前经 Wi-Fi 连接——V1 仅支持 USB："
                    "请插数据线后重试")
            return d["udid"]
    known = ", ".join(f"{(d.get('name') or '?')}={d['udid']}" for d in devs) or "（无已配对真机）"
    raise RuntimeError(
        f"不认识的 iOS 设备标识 {serial!r}：真机请传 udid（当前设备：{known}；"
        "`mat ios-setup` 可查看），模拟器请传 36 位模拟器 udid 或省略 --serial")


def ios_like_name(dev: dict) -> str:
    """人读设备名（describe 的名字部分）：营销名 → 设备名 → udid。"""
    return dev.get("model") or dev.get("name") or dev.get("udid") or "?"


def parse_team_id(subject: str) -> str | None:
    """从证书 subject 里取 Team ID（`OU=` 字段）。纯函数，便于单测。

    实测：`CN=Apple Development: … (PPQC44YHLR), OU=U56RW5ZM8J` ⇒ `U56RW5ZM8J`。
    """
    for part in (subject or "").split(","):
        part = part.strip()
        if part.startswith("OU="):
            value = part[3:].strip()
            m = _TEAM_RE.search(value)
            if m:
                return m.group(1)
    return None


def team_id() -> str | None:
    """从钥匙串里**第一个** Apple Development 证书读 Team ID。

    为什么能这么读：证书的 `OU` 就是团队标识（Apple 的固定编码方式），比让用户手填可靠。
    无证书 / 无 openssl ⇒ `None`（上层提示"请先在 Xcode 登录 Apple ID"）。
    """
    try:
        cert = _run(["security", "find-certificate", "-c", "Apple Development", "-p"])
        if cert.returncode != 0 or "BEGIN CERTIFICATE" not in (cert.stdout or ""):
            return None
        info = subprocess.run(["openssl", "x509", "-noout", "-subject"], input=cert.stdout,
                              capture_output=True, text=True, timeout=_TIMEOUT)
        return parse_team_id(info.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return None


def install_app(udid: str, path: str | Path, timeout: int = 180) -> tuple[bool, str]:
    """装 App（`devicectl` **只接受 `.app` 目录**，`.ipa` 需先解压——实测其 help 明写）。

    返回 (成功?, 失败原因)。失败原因取**最后一行 error**，不整段回显（devicectl 输出很长）。
    """
    try:
        proc = _run(["xcrun", "devicectl", "device", "install", "app",
                     "--device", udid, str(path)], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, ""
    blob = (proc.stderr or "") + (proc.stdout or "")
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    return False, (lines[-1] if lines else "安装失败（无输出）")[:300]


def launch_app(udid: str, bundle_id: str, timeout: int = 60) -> tuple[bool, str]:
    """用 `devicectl` 启动 App。**WDA 的 app_launch 兜底**。

    为什么需要：实测（2026-10-18）当被测 App 的 bundle id 与 WDA test host **同 id / 同前缀**时，
    WDA 的 `app_launch` 会**静默失败**（不报错，前台仍是 springboard），而 devicectl 能起来。
    返回值同 install/uninstall：失败原因取末行。
    """
    try:
        proc = _run(["xcrun", "devicectl", "device", "process", "launch",
                     "--device", udid, bundle_id], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, ""
    blob = (proc.stderr or "") + (proc.stdout or "")
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    return False, (lines[-1] if lines else "启动失败（无输出）")[:300]


def uninstall_app(udid: str, bundle_id: str, timeout: int = 120) -> tuple[bool, str]:
    """卸载 App（`--fresh-install` 的第一步）。失败原因同上取末行。"""
    try:
        proc = _run(["xcrun", "devicectl", "device", "uninstall", "app",
                     "--device", udid, bundle_id], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, ""
    blob = (proc.stderr or "") + (proc.stdout or "")
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    return False, (lines[-1] if lines else "卸载失败（无输出）")[:300]


#: 描述文件可能落在这两处（Xcode 新位置 / 旧位置）
_PROFILE_DIRS = (
    Path.home() / "Library" / "Developer" / "Xcode" / "UserData" / "Provisioning Profiles",
    Path.home() / "Library" / "MobileDevice" / "Provisioning Profiles",
)


def _profile_meta(path: Path) -> dict | None:
    """解一份 .mobileprovision 的元数据（只读，不解私钥）。失败返回 None。"""
    try:
        proc = _run(["security", "cms", "-D", "-i", str(path)])
        if proc.returncode != 0 or not proc.stdout:
            return None
        return plistlib.loads(proc.stdout.encode())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def days_left(expires: datetime, now: datetime | None = None) -> int:
    """剩余天数（向下取整，可为负）。纯函数，便于单测。"""
    now = now or datetime.now(expires.tzinfo)
    return int((expires - now).total_seconds() // 86400)


def profile_expiry(metas: list[dict] | None = None) -> dict | None:
    """**最早过期**的开发 profile。返回 `{name, app_id, expires, days_left}`，无则 None。

    为什么要它：免费个人证书**只有 7 天**，过期后真机跑测直接失败、且报错与"没签名"混在一起
    （`No profiles for ...`）。提前提醒比事后排查便宜得多。
    """
    if metas is None:
        metas = []
        for d in _PROFILE_DIRS:
            if not d.exists():
                continue
            for f in sorted(d.glob("*.mobileprovision")):
                meta = _profile_meta(f)
                if meta:
                    metas.append(meta)
    items: list[dict] = []
    for meta in metas:
        expires = meta.get("ExpirationDate")
        if not isinstance(expires, datetime):
            continue
        ents = meta.get("Entitlements") or {}
        items.append({
            "name": meta.get("Name", ""),
            "app_id": ents.get("application-identifier", ""),
            "expires": expires,
            "days_left": days_left(expires),
        })
    if not items:
        return None
    valid = [i for i in items if i["days_left"] >= 0]
    if valid:
        # 有效里**最先到期**的那份才是"下一个会坏的东西"
        return min(valid, key=lambda i: i["days_left"])
    # 全都过期：报**最晚过期**的那份（最接近可用），并明确标记已过期
    worst = max(items, key=lambda i: i["days_left"])
    return {**worst, "expired": True}


def bundle_id_of(app_path: str | Path) -> str | None:
    """读 `.app/Info.plist` 的 `CFBundleIdentifier`（`--fresh-install` 拿卸载目标用）。

    `devicectl install app` 只接受 `.app` 目录，所以这里也按目录读；读不到返回 None
    （调用方报"这不是一个 .app 目录"比猜一个 bundle id 好）。
    """
    try:
        with (Path(app_path) / "Info.plist").open("rb") as f:
            return (plistlib.load(f) or {}).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def describe(dev: dict, with_transport: bool = True) -> str:
    """人读一行：`iPhone 17 Pro · iOS 26.5.2 · USB`（doctor / ios-setup 共用）。

    transport 映射成人话（2026-10-22）：devicectl 原始值 `localNetwork` 用户看不懂，
    统一显示「Wi-Fi（无线调试）」——它只说明设备在 Xcode 里勾了「通过网络连接」，
    不代表能跑 WDA（执行只认 USB）。
    """
    parts = [dev.get("model") or dev.get("name") or dev["udid"]]
    if dev.get("os_version"):
        parts.append(f"iOS {dev['os_version']}")
    if with_transport and dev.get("transport"):
        t = dev["transport"]
        parts.append("USB" if t == "wired" else
                     ("Wi-Fi（无线调试）" if t == "localNetwork" else t))
    return " · ".join(parts)


def xcode_account_count(plist_path: Path | None = None) -> int:
    """磁盘上 Xcode 偏好里的 Apple ID 数量（**以磁盘为准**，见下面的坑）。

    ⚠️ 2026-10-17 实测的坑：在 Xcode 里加了账号后，`defaults read` 能看到 1 个，
    但 `~/Library/Preferences/com.apple.dt.Xcode.plist` 里仍是空数组——
    **`xcodebuild` 读不到未落盘的账号**，一直报 `No accounts`。Xcode 退出后才落盘
    （若登录没完成，退出会直接丢弃）。
    ⇒ 判"能不能签名"必须读**磁盘**这份，不能读 cfprefsd 缓存。
    """
    path = plist_path or (Path.home() / "Library" / "Preferences" / "com.apple.dt.Xcode.plist")
    try:
        with path.open("rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return 0
    lists = data.get("DVTDeveloperAccountManagerAppleIDLists") or {}
    ids = lists.get("IDE.Identifiers.Prod") or []
    return len(ids) if isinstance(ids, list) else 0
