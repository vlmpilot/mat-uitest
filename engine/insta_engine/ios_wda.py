"""WDA 一键启动（iOS 模拟器 + 真机，发起人 2026-10-12：概览红点只有 tips 没有入口）。

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
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

#: WDA runner 的 bundle id（appium/WebDriverAgent 源码构建产物）。
#: ⚠️ 后缀是 **xctrunner**（Xcode 给 UI 测试宿主加的后缀），**不是 xctruntime**——
#: 2026-10-17 实测：写成 xctruntime 时 `simctl launch` 必失败（"failed to launch"），
#: 于是快路径每次都静默落到构建兜底（"装过 WDA 就秒起"从未生效）。
#: 真机上该 id 还可能被签名团队改写（个人团队无法注册 com.facebook.* 前缀，
#: 实测设备上是 com.<team>.WebDriverAgentRunner.xctrunner）⇒ 真机启动不走本常量。
WDA_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctrunner"
WDA_STATUS_URL = "http://localhost:8100/status"

#: WDA 源码与构建产物缓存（~/.mat 与 license/usage 同根；随版本升级可整目录删除重建）
#:
#: **版本锁定（2026-10-19，审查 R5）**：此前拉 `refs/heads/master`，上游一次不兼容
#: 更新就会让所有用户的 iOS 全线崩。现在锁 release tag；升级 = 改 `WDA_REF`，
#: 缓存目录随之隔离（老版本的目录留着不互相污染，可整目录删除回收）。
#: v16.15.3 = 2026-10-16 发布（真机调测同期的最新稳定版）。
WDA_REF = "v16.15.3"
WDA_SRC_DIR = Path.home() / ".mat" / "wda" / f"WebDriverAgent-{WDA_REF}"
WDA_TARBALL_URL = f"https://codeload.github.com/appium/WebDriverAgent/tar.gz/refs/tags/{WDA_REF}"

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
    # **解到临时目录再搬**：此前直接解到 base 再 `base.glob("WebDriverAgent-*")` 取第一个，
    # 升级 WDA_REF 后 base 里还有旧版本目录（如 `WebDriverAgent-master`），glob 可能挑中旧的
    # ⇒ 把旧源码改名成新版本名，构建出**上一版 WDA**，而界面上看不出任何异常。
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        subprocess.run(["tar", "-xzf", str(tgz), "-C", tmp], check=True, timeout=120)
        children = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(children) != 1:
            raise RuntimeError(f"WDA 源码包结构异常（顶层 {len(children)} 个目录）")
        children[0].rename(WDA_SRC_DIR)


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
        # 该动作本机只拉模拟器；真机走 start_on_device（doctor 按有无 USB 真机分流）
        return False, "没有已启动的模拟器或已连接的真机"
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


# ============================ 真机（P2） =====================================
# 与模拟器的三处本质差异，决定了下面每个函数都要单开一条路径：
# 1) **没有 simctl**：起服务只能靠 xcodebuild（build-for-testing → test-without-building）；
# 2) **必须签名**：个人团队注册不了 `com.facebook.*` 前缀 ⇒ runner 的 bundle id 要改写
#    （实测设备上那个 `com.vizenluan.WebDriverAgentRunner.xctrunner` 就是前人手工改的产物）；
# 3) **服务不在 localhost**：真机 8100 端口要走 usbmux（`wda.USBClient`），不能 `urlopen`。

#: 真机构建产物目录（与模拟器的 `dd` 分开，避免架构/xctestrun 互相覆盖）
WDA_DEVICE_DD = Path.home() / ".mat" / "wda" / "dd-device"

#: 真机 WDA 启动日志（签名失败的原因只在 stderr 里，必须留档）
WDA_DEVICE_LOG_DIR = Path.home() / ".mat" / "wda" / "logs"


def default_runner_bundle_id(team: str) -> str:
    """个人团队可用的 runner bundle id：`com.<team>.WebDriverAgentRunner`。

    为什么不用 `com.facebook.*`：App ID 在 Apple 侧**全局唯一**，Facebook 已注册该前缀，
    个人团队再注册必然失败（实测 `No profiles for 'com.vizenluan…'` 前身就是这个问题）。
    用团队 id 做前缀既保证唯一，又不用用户想名字。
    """
    return f"com.{team.lower()}.WebDriverAgentRunner"


def _patch_runner_bundle_id(bundle_id: str) -> int:
    """把 WDA 工程里 **iOS runner** 的 bundle id 改成 `bundle_id`，返回改了几处。

    只改带 `USES_XCTRUNNER = YES` 的那两个配置（Debug/Release）——工程里还有 tvOS/watchOS
    的同名 target，按行号改会在上游更新后错位。**幂等**：已是目标值就不动。
    """
    pbx = WDA_SRC_DIR / "WebDriverAgent.xcodeproj" / "project.pbxproj"
    lines = pbx.read_text(encoding="utf-8").split("\n")
    changed = 0
    for i, line in enumerate(lines):
        if "PRODUCT_BUNDLE_IDENTIFIER = " not in line:
            continue
        if "USES_XCTRUNNER = YES" not in "\n".join(lines[i:i + 4]):
            continue
        new = re.sub(r"PRODUCT_BUNDLE_IDENTIFIER = [^;]+;",
                     f"PRODUCT_BUNDLE_IDENTIFIER = {bundle_id};", line)
        if new != line:
            lines[i] = new
            changed += 1
    if changed:
        pbx.write_text("\n".join(lines), encoding="utf-8")
    return changed


#: 构建/启动失败的**分类**：每条都映射到用户能自己做的动作（不让用户看原始 stderr）
_DEVICE_ERRORS = (
    (("no accounts",), "Xcode 没登录 Apple ID——Xcode → Settings → Accounts 登录后重试"),
    (("no profiles for", "doesn't include signing certificate"),
     "自动签名失败——确认账号下有 Personal Team；免费证书 7 天过期后需重签"),
    (("requires a development team",), "未指定开发团队——用 --team 指定，或先在 Xcode 登录"),
    (("not paired", "unable to connect", "could not connect", "tunnel"),
     "设备不可达——用数据线连接、解锁，并在弹窗点「信任」"),
    (("developer mode",), "设备未开启开发者模式——设置 → 隐私与安全性 → 开发者模式"),
    (("not trusted", "certificate is not trusted", "untrusted developer"),
     "设备未信任开发证书——设置 → 通用 → VPN 与设备管理 → 开发者 App → 信任（免费证书首次必做）"),
    (("is locked", "locked"), "设备已锁屏——解锁后重试"),
)


def _classify_device_error(blob: str) -> str:
    low = (blob or "").lower()
    for keys, hint in _DEVICE_ERRORS:
        if any(k in low for k in keys):
            return hint
    return ""


def _build_wda_device(udid: str, team: str, bundle_id: str) -> Path:
    """真机 build-for-testing（签名 + 编译），返回 xctestrun 路径。"""
    WDA_DEVICE_DD.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["xcodebuild", "-project", str(WDA_SRC_DIR / "WebDriverAgent.xcodeproj"),
         "-scheme", "WebDriverAgentRunner",
         "-destination", f"id={udid}",
         "-derivedDataPath", str(WDA_DEVICE_DD),
         "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration",
         f"DEVELOPMENT_TEAM={team}", "CODE_SIGN_STYLE=Automatic",
         "build-for-testing"],
        capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        blob = (proc.stderr or "") + (proc.stdout or "")
        hint = _classify_device_error(blob)
        raise RuntimeError(hint or f"真机构建失败：{_last_error_line(proc)}")
    xruns = sorted((WDA_DEVICE_DD / "Build" / "Products").glob("*.xctestrun"))
    if not xruns:
        raise RuntimeError("真机构建完成但未找到 .xctestrun 产物")
    return xruns[0]


def device_wda_ready(udid: str) -> bool:
    """真机 WDA 是否就绪：走 usbmux（`USBClient`），**不能用 localhost**。

    2026-10-22 实测：真机屏幕已显示 Automation Running，但 localhost:8100 探不到——
    旧判据只探 localhost，把「真机已就绪」误报成「WDA 服务未起」。
    失败即未就绪（体检/环境卡语义），不抛。
    """
    try:
        import wda  # 延迟导入：没装 [ios] extra 也能跑体检
    except ImportError:
        return False
    try:
        return bool(wda.USBClient(udid=udid).is_ready())
    except Exception:  # noqa: BLE001 —— 连不上就是没就绪
        return False


def _start_wda_device_service(udid: str, xctestrun: Path) -> None:
    """`test-without-building` 分离进程拉起（真机 WDA 服务本体，必须常驻）。

    日志落 `~/.mat/wda/logs/<udid>.log`——真机首次签名/启动失败的原因只在 stderr 里，
    没有这份日志，用户只能看到"没起来"。

    ⚠️ 日志文件写不了**不能阻断启动**（2026-10-22 实测：沙箱/权限导致 open 抛
    PermissionError，直接冒到前端"拉起 WDA 服务失败"，设备明明是可用的）。
    降级顺序：正式日志目录 → 系统临时目录 → 丢弃输出。
    """
    log = None
    for candidate in (WDA_DEVICE_LOG_DIR / f"{udid}.log",
                      Path(tempfile.gettempdir()) / f"mat-wda-{udid}.log"):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            log = open(candidate, "w")  # noqa: SIM115 —— 交给子进程持有
            break
        except OSError:
            continue
    kwargs: dict = {
        "stdout": log if log is not None else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if log is not None else subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                          "-destination", f"id={udid}"], **kwargs)
    finally:
        # 子进程已继承自己的 fd，父进程这份必须关——否则每起一次泄一个 fd
        # （控制台长跑反复启停会积累到 EMFILE）。
        if log is not None:
            log.close()


def _poll_device_status(udid: str, seconds: int) -> bool:
    """轮询**真机** WDA 就绪（走 usbmux；不能用 localhost）。"""
    try:
        import wda  # 延迟导入：无 [ios] extra 时本模块仍可被 import
    except ImportError:
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if wda.USBClient(udid=udid).is_ready():
                return True
        except Exception:  # noqa: BLE001 —— 轮询语义：失败即未就绪
            pass
        time.sleep(2)
    return False


def start_on_device(udid: str, team: str | None = None, wait_seconds: int = 240,
                    allow_build: bool = True) -> tuple[bool, str]:
    """真机拉起 WDA：签名构建（首次）→ 常驻服务 → 轮询就绪。

    - `team` 缺省从钥匙串证书的 `OU` 读（`ios_device.team_id()`）；
    - `allow_build=False` 用于"已构建过"的快路径（产物在 `WDA_DEVICE_DD`）；
    - 返回 `(是否就绪, 给人看的详情)`，**永不抛**——CLI / console 都靠它渲染。
    """
    from .ios_device import team_id as _team_id

    # ① **优先复用 Xcode 已签名的产物**（2026-10-18 实测）：不需要账号、不需要 ⌘U。
    #    本机 xcodebuild 用不了账号，但 GUI 构建的产物是签好的——这是唯一能自动化的路径。
    reused = _try_reuse_signed_runner(udid, wait_seconds)
    if reused is not None and reused[0]:
        return True, reused[1]

    team = team or _team_id()
    if not team:
        return False, ("没找到签名团队——Xcode → Settings → Accounts 登录 Apple ID"
                       "（真机签名必须有团队）")
    try:
        _ensure_wda_source()
    except (OSError, subprocess.SubprocessError, StopIteration) as exc:
        return False, f"WDA 源码下载失败：{type(exc).__name__}: {exc}"

    bundle_id = default_runner_bundle_id(team)
    try:
        _patch_runner_bundle_id(bundle_id)
    except OSError as exc:
        return False, f"改写 runner bundle id 失败：{exc}"

    xruns = sorted((WDA_DEVICE_DD / "Build" / "Products").glob("*.xctestrun"))
    if not xruns:
        if not allow_build:
            return False, "没有真机构建产物——先跑一次 `mat ios-setup`（首次需构建签名）"
        try:
            xctestrun = _build_wda_device(udid, team, bundle_id)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
    else:
        xctestrun = xruns[0]

    try:
        _start_wda_device_service(udid, xctestrun)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"拉起 WDA 服务失败：{type(exc).__name__}: {exc}"

    if _poll_device_status(udid, wait_seconds):
        return True, f"真机 WDA 已就绪（{udid}，bundle={bundle_id}）"
    log = WDA_DEVICE_LOG_DIR / f"{udid}.log"
    tail = ""
    try:
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:])
    except OSError:
        pass
    hint = _classify_device_error(tail)
    return False, ("WDA 已拉起但设备 8100 未就绪" + (f"：{hint}" if hint else "")
                   + (f"（日志 {log}）" if log.exists() else ""))


# ==================== 复用 Xcode 已签名产物（2026-10-18 实测） ====================
# 背景：本机 `xcodebuild -allowProvisioningUpdates` 用不了账号（No Accounts），
# 但 **Xcode GUI 构建出的产物是已签名的**。只要那份产物还在 DerivedData 里，
# 就能把模拟器 xctestrun 改造成真机版，用 `test-without-building` 直接起服务——
# **不需要账号，也不需要用户再按一次 ⌘U**（实测 2026-10-18：WDA 1 秒内就绪）。

#: Xcode 的 DerivedData 根（找 ⌘U 构建产物）
_DERIVED_DATA = Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"


def _runner_identifier(app: Path) -> str:
    """`codesign -dv` 里的 Identifier（写 stderr）。取不到返回空串。"""
    try:
        proc = subprocess.run(["codesign", "-dv", str(app)],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (proc.stderr or "").splitlines():
        if line.startswith("Identifier="):
            return line.split("=", 1)[1].strip()
    return ""


def _runner_profile_expiry(app: Path) -> datetime | None:
    """产物内嵌 profile 的过期时间；解析失败返回 None（**不因解析失败而排除**）。"""
    try:
        from .ios_device import _profile_meta

        meta = _profile_meta(app / "embedded.mobileprovision")
    except Exception:  # noqa: BLE001 —— 解析失败按"未知"处理
        return None
    expires = (meta or {}).get("ExpirationDate")
    return expires if isinstance(expires, datetime) else None


def find_signed_device_runner() -> tuple[Path, str] | None:
    """找**已签名且未过期**的真机 WDA runner，**取最新的那份**。

    判据：有 `embedded.mobileprovision`（否则装不上）+ `codesign` Identifier 以
    `.xctrunner` 结尾（否则不是 UI test runner）+ **profile 未过期**。

    ⚠️ 2026-10-20 实测的坑：DerivedData 里同时存在多份产物（换团队/重装过），
    旧实现按目录名 `sorted()` 取第一个 ⇒ 挑中了 9 月那份（`com.vizenluan.*`，
    profile **9-25 已过期**），而当天有效的 `com.u56rw5zm8j.*` 被忽略。
    结果是"复用"必然失败、且报错指向签名，完全看不出是选错了产物。
    ⇒ 先排除过期，再按 **mtime 倒序**取最新。
    """
    pattern = "WebDriverAgent-*/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    best: tuple[float, Path, str] | None = None
    for app in _DERIVED_DATA.glob(pattern):
        if not (app / "embedded.mobileprovision").exists():
            continue
        ident = _runner_identifier(app)
        if not ident.endswith(".xctrunner"):
            continue
        expires = _runner_profile_expiry(app)
        if expires is not None and expires < datetime.now(expires.tzinfo):
            continue          # 过期产物复用必失败，直接排除
        try:
            mtime = app.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, app, ident)
    return (best[1], best[2]) if best else None


def _template_xctestrun() -> Path | None:
    """拿一个模拟器 xctestrun 当模板（结构完全一致，只换路径与 bundle id）。"""
    for base in (WDA_SRC_DIR / "dd" / "Build" / "Products",):
        hits = sorted(base.glob("*iphonesimulator*.xctestrun"))
        if hits:
            return hits[0]
    hits = sorted(_DERIVED_DATA.glob("WebDriverAgent-*/Build/Products/*iphonesimulator*.xctestrun"))
    return hits[0] if hits else None


def craft_device_xctestrun(app: Path, bundle_id: str, template: Path, out: Path) -> Path:
    """模拟器 xctestrun → 真机版（`__TESTROOT__` 指向同目录的 `Debug-iphoneos`）。

    两处必须改对，否则报错很难懂：
    ① `TestHostBundleIdentifier` 用**产物实际的** bundle id（个人团队是 `com.<team>.*`）；
    ② 去掉 `DYLD_INSERT_LIBRARIES`（模拟器专用：libMainThreadChecker / `__SIMRUNTIMEROOT__`）。
    """
    import plistlib

    data = plistlib.loads(template.read_bytes())
    key = next((k for k in data if "WebDriverAgent" in k), None)
    if not key:
        raise RuntimeError("模板 xctestrun 结构不认识（找不到 WebDriverAgent* 段）")
    cfg = data[key]
    env: dict = {}
    for k, v in (cfg.get("TestingEnvironmentVariables") or {}).items():
        if k == "DYLD_INSERT_LIBRARIES":
            continue
        env[k] = (v.replace("Debug-iphonesimulator", "Debug-iphoneos")
                   .replace("iPhoneSimulator.platform", "iPhoneOS.platform"))
    cfg["TestingEnvironmentVariables"] = env
    cfg["DependentProductPaths"] = [
        p.replace("Debug-iphonesimulator", "Debug-iphoneos")
        for p in (cfg.get("DependentProductPaths") or [])]
    cfg["TestHostPath"] = "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    cfg["TestHostBundleIdentifier"] = bundle_id
    cfg["IsUITestBundle"] = True

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(plistlib.dumps(data))
    return out


#: 本地 profile 备份目录（强制续期前先备份，失败要能回滚）
_PROFILE_BACKUP_DIR = Path.home() / ".mat" / "wda" / "profile-backup"


def _runner_profile_files(team: str) -> list[Path]:
    """属于本团队 WDA runner 的本地 profile 文件（base 与 .xctrunner 都算）。"""
    from .ios_device import _PROFILE_DIRS, _profile_meta

    out: list[Path] = []
    for d in _PROFILE_DIRS:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.mobileprovision")):
            meta = _profile_meta(f)
            app_id = ((meta or {}).get("Entitlements") or {}).get("application-identifier", "")
            if "WebDriverAgentRunner" in app_id and team in app_id:
                out.append(f)
    return out


def _backup_and_remove_profiles(team: str) -> Path | None:
    """把 runner 的 profile 备份后删除。返回备份目录（无可删时返回 None）。

    ⚠️ 这是**破坏性动作**：删完本地就没有可用的 profile 了，必须能拿回新的。
    所以调用方要先确认账号可用，并在失败时 `_restore_profiles`。
    """
    import shutil
    from datetime import datetime as _dt

    files = _runner_profile_files(team)
    if not files:
        return None
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    backup = _PROFILE_BACKUP_DIR / stamp
    backup.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, backup / f.name)
        f.unlink()
    return backup


def _restore_profiles(backup: Path | None) -> None:
    """回滚：把备份的 profile 放回原目录（按文件名，回到第一个存在的目录）。"""
    import shutil

    if backup is None or not backup.exists():
        return
    from .ios_device import _PROFILE_DIRS

    target = next((d for d in _PROFILE_DIRS if d.exists()), _PROFILE_DIRS[0])
    target.mkdir(parents=True, exist_ok=True)
    for f in backup.glob("*.mobileprovision"):
        shutil.copy2(f, target / f.name)


def renew_device_profile(udid: str, team: str | None = None,
                         force: bool = False) -> tuple[bool, str]:
    """**尝试自动续期**：重新签名构建，刷新 7 天有效期。

    免费个人证书的"续期"没有单独的 API——**重新签名一次就是续期**（Xcode 会签发新 profile）。
    所以这里复用的就是 `build-for-testing -allowProvisioningUpdates`。

    ⚠️ 能做到什么程度取决于 **xcodebuild 能不能用账号**：
    - 能用 ⇒ 全自动（实测本机不能用：`No accounts`，只能走 Xcode GUI 按一次 ⌘U）；
    - 不能用 ⇒ 如实返回失败原因 + 手动步骤，**不假装成功**。
    """
    from .ios_device import profile_expiry
    from .ios_device import team_id as _team_id

    team = team or _team_id()
    if not team:
        return False, ("自动续期失败：没有签名团队——先在 Xcode → Settings → Accounts 登录 Apple ID")
    from .ios_device import xcode_account_count

    before = profile_expiry()
    backup = None
    if force:
        # **提前换新**必须删掉旧 profile：Apple 在旧 profile 仍有效时不会重发，
        # Xcode 只会复用它（实测：剩 3 天时重签，到期时间纹丝不动）。
        # 但删了就得能拿回新的 ⇒ 先确认账号可用，否则拒绝，别把"还能跑 3 天"变成"现在就跑不了"。
        if xcode_account_count() <= 0:
            return False, (
                "拒绝强制续期：xcodebuild 用不了账号（未登录），删掉旧 profile 后拿不到新的，"
                "设备会**立刻**不可用。请先在 Xcode 登录 Apple ID 并退出让它落盘，再重试")
        backup = _backup_and_remove_profiles(team)
    try:
        _ensure_wda_source()
        _patch_runner_bundle_id(default_runner_bundle_id(team))
        _build_wda_device(udid, team, default_runner_bundle_id(team))
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        _restore_profiles(backup)
        msg = str(exc)
        if "no account" in msg.lower():
            return False, ("自动续期失败：xcodebuild 用不了账号（No accounts）——"
                           "只有 Xcode GUI 能签。请在 Xcode 里对 WebDriverAgentRunner 按一次 ⌘U")
        return False, f"自动续期失败：{msg}"
    after = profile_expiry()
    if not after:
        return False, "构建成功但没读到有效期——请用 mat ios-setup 复查"
    when = after["expires"].astimezone().strftime("%m-%d %H:%M")
    # ⚠️ **判定必须比 before/after**：构建返回 0 不等于续期成功。实测（2026-10-20）
    # profile 还剩 3 天时，Xcode 直接复用它、不重签，有效期纹丝不动——旧判定把这种
    # 情况报成"续期成功"，是典型的假成功。
    if before and after["days_left"] <= before["days_left"]:
        _restore_profiles(backup)   # 强制换新失败 ⇒ 把旧 profile 放回去
        return False, (
            f"构建完成，但证书**没有变化**（仍剩 {after['days_left']} 天，{when} 到期）。"
            "Apple 只在必要时签发新 profile：证书还有效时重签不会延长期限。"
            "到期后重跑本命令，或在 Xcode 里对 WebDriverAgentRunner 按一次 ⌘U")
    return True, (f"已签发新证书：到期 {when}，剩 {after['days_left']} 天"
                  + (f"（原 {before['days_left']} 天）" if before else ""))


def _try_reuse_signed_runner(udid: str, wait_seconds: int) -> tuple[bool, str] | None:
    """尝试用已签名产物起服务。`None` = 没有可用产物（调用方走构建路径）。"""
    # **已经就绪就别再起一次**：重启会把正在跑的会话打断（控制台点"启动 WDA"、
    # 或 ios-setup --start 时尤其明显），而且白等最多 90s。
    if _poll_device_status(udid, 1):
        return True, "真机 WDA 已就绪（未重启服务）"
    found = find_signed_device_runner()
    if not found:
        return None
    app, bundle_id = found
    template = _template_xctestrun()
    if not template:
        return None
    try:
        xctestrun = craft_device_xctestrun(
            app, bundle_id, template,
            app.parent / "WebDriverAgentRunner_iphoneos-crafted.xctestrun")
        _start_wda_device_service(udid, xctestrun)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return False, f"复用已签名产物失败：{type(exc).__name__}: {exc}"
    if _poll_device_status(udid, min(wait_seconds, 90)):
        return True, f"真机 WDA 已就绪（复用已签名产物 {bundle_id}，无需账号）"
    return False, "复用已签名产物启动后 90s 内未就绪"
