"""被测 App 安装域（测试准入）：/api/apps/install、/api/apps/uninstall。

- Android：`adb install -r -t`（覆盖安装保留数据；-t 允许测试包）；卸载 `adb uninstall`。
- iOS 模拟器：`xcrun simctl install booted <path.app>`；卸载 `simctl uninstall`。
- iOS 真机：签名/开发者模式 ⇒ P2，安装/卸载均不可用（诚实拒绝）。
- 安装成功可选回显 versionName + APK sha256 指纹（T76 准入证据链）——需要调用方
  提供包名（APK 路径里没有包名，不做 aapt 解析）。

⚠️ 卸载是**破坏性设备操作**：`_local_only` 门控——`--share` 局域网暴露时禁用。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..device import list_adb_devices
from ..driver import app_apk_sha256, app_version
from .runtime import _require_fresh_code, get_driver, operator_of, start_job
from .workspace import _local_only


def _adb(serial: str | None, *args: str) -> list[str]:
    return ["adb"] + (["-s", serial] if serial else []) + list(args)


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or ""))


def _fingerprint(drv, package: str, platform: str) -> dict:
    """版本号 + 包指纹（T76）；取不到就空串（绝不影响主流程）。iOS 暂无对应物。"""
    if platform != "android":
        return {"version": "", "sha256": ""}
    try:
        return {"version": app_version(drv, package),
                "sha256": app_apk_sha256(drv, package)}
    except Exception:
        return {"version": "", "sha256": ""}


def api_apps_install(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/apps/install：安装测试包（job 化——大 APK 安装可达 1 分钟）。"""
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    path = str(body.get("path", "")).strip()
    package = str(body.get("package", "")).strip() or None
    if platform == "ios" and serial and serial != "simulator":
        return 400, {"error": "iOS 真机安装需要签名与开发者模式——属 P2（当前仅模拟器支持）"}
    p = Path(path).expanduser()
    if not p.exists():
        return 404, {"error": f"文件不存在: {p}"}
    if platform == "android" and p.suffix.lower() != ".apk":
        return 400, {"error": f"Android 安装包须为 .apk（收到 {p.suffix or '无后缀'}）"}
    if platform == "ios" and p.suffix.lower() not in (".app", ".ipa"):
        return 400, {"error": f"iOS 模拟器安装须为 .app 目录（收到 {p.suffix or '无后缀'}）"}

    def job(emit, cancel):
        _require_fresh_code()
        if platform == "android":
            emit("install", "running", f"adb install -r -t {p.name}（覆盖安装，保留数据）")
            rc, out = _run(_adb(serial, "install", "-r", "-t", str(p)), timeout=600)
            if rc != 0 or "Success" not in out:
                raise RuntimeError(f"安装失败: {out[-300:]}")
        else:
            emit("install", "running", f"simctl install {p.name}")
            rc, out = _run(["xcrun", "simctl", "install", "booted", str(p)], timeout=300)
            if rc != 0:
                raise RuntimeError(f"安装失败: {out[-300:]}")

        summary = f"安装成功: {p.name}"
        fingerprint = {}
        if package:
            try:
                drv = get_driver(serial, platform)
                fingerprint = _fingerprint(drv, package, platform)
                if fingerprint.get("version"):
                    summary += f" · 版本 {fingerprint['version']}"
                if fingerprint.get("sha256"):
                    summary += f" · 指纹 {fingerprint['sha256'][:12]}"
            except Exception:
                pass
        return summary, None

    return 200, {"job_id": start_job("install", job, meta={"serial": serial, "platform": platform,
                                                           "operator": operator_of(body)})}


def api_app_probe(path: str) -> tuple[int, dict]:
    """GET /api/app/probe?path=...：从**本地安装包**解析包名（安装弹窗自动回填，2026-10-13）。

    背景：装完测试包用户不知道包名就没法建用例/跑测试（发起人原话）。包名其实就在
    安装包本体里——iOS .app 读 Info.plist（标准库 plistlib，含二进制格式），Android .apk
    用 aapt dump badging（复用 `_aapt_bin` 探测，与显示名解析同一来源）。
    **失败一律静默回空**（200）——这是增强能力，解析不出就让用户手填，不弹错。

    本机路径与 /api/fs 浏览器同信任模型（配置台是本机服务），不做目录白名单。
    """
    p = Path(path).expanduser()
    empty = {"package": "", "label": "", "version": ""}
    if not p.exists():
        return 200, dict(empty)
    if p.suffix.lower() == ".app" or (p.is_dir() and (p / "Info.plist").exists()):
        result = dict(empty)
        try:
            import plistlib
            with (p / "Info.plist").open("rb") as fh:
                info = plistlib.load(fh)
            result = {"package": str(info.get("CFBundleIdentifier") or ""),
                      "label": str(info.get("CFBundleDisplayName")
                                    or info.get("CFBundleName") or ""),
                      "version": str(info.get("CFBundleShortVersionString") or "")}
        except Exception:  # 非标准 plist 也可能出现在真实磁盘上（实测 demo 构建脚本会在尾部追加说明文字）——继续走正则兜底
            pass
        if not all(result.values()):
            try:  # 兜底：对残缺 XML 正则直提缺失字段（`<key>K</key>\n<string>V</string>`）
                text = (p / "Info.plist").read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            for key, field in (("CFBundleIdentifier", "package"),
                               ("CFBundleDisplayName", "label"),
                               ("CFBundleName", "label"),
                               ("CFBundleShortVersionString", "version")):
                if result[field]:
                    continue
                m = re.search(rf"<key>{key}</key>\s*<string>([^<]+)</string>", text)
                if m:
                    result[field] = m.group(1).strip()
        return 200, result
    if p.suffix.lower() == ".apk":
        aapt = _aapt_bin()
        if not aapt:
            return 200, dict(empty)
        try:
            _, badging = _run([aapt, "dump", "badging", str(p)], timeout=30)
            pm = re.search(r"^package: name='([^']+)'", badging, re.MULTILINE)
            lm = re.search(r"^application-label:'([^']*)'", badging, re.MULTILINE)
            vm = re.search(r"versionName='([^']*)'", badging)
            return 200, {"package": pm.group(1) if pm else "",
                         "label": lm.group(1).strip() if lm else "",
                         "version": vm.group(1) if vm else ""}
        except (OSError, subprocess.SubprocessError):
            return 200, dict(empty)
    return 200, dict(empty)


def api_apps_uninstall(root: Path, host: str, body: dict) -> tuple[int, dict]:
    """POST /api/apps/uninstall：卸载被测 App（清数据语义——重装即全新状态）。

    ⚠️ `_local_only`：卸载是破坏性设备操作，`--share` 局域网暴露时禁用。"""
    gate = _local_only(host)
    if gate:
        return gate
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    package = str(body.get("package", "")).strip()
    if not package or " " in package or ";" in package or "&" in package:
        return 400, {"error": "需要合法的包名/bundleId"}
    if platform == "ios" and serial and serial != "simulator":
        return 400, {"error": "iOS 真机卸载需要签名与开发者模式——属 P2"}

    if platform == "android":
        rc, out = _run(_adb(serial, "uninstall", package), timeout=120)
        ok = rc == 0 and "Success" in out
    else:
        rc, out = _run(["xcrun", "simctl", "uninstall", "booted", package], timeout=120)
        ok = rc == 0
    if not ok:
        return 400, {"error": f"卸载失败（可能未安装）: {out[-200:]}"}
    return 200, {"ok": True, "package": package}


# -- App 显示名（发起人 2026-10-12：用例列表按包名分组，包名可读性差）--------------------
# 显示名（launcher label）不在 dumpsys/pm 输出里，只能从 APK 本体解析：
# pm path → `adb exec-out cat` 拉到临时文件 → aapt/aapt2 dump badging。
# 每个包名只解析一次，落缓存 artifacts/.app_labels.json；无 aapt/无设备/解析失败
# 一律静默回空串（前端回落包名，绝不报错）。

def _aapt_bin() -> str:
    """aapt2/aapt 探测：ANDROID_HOME build-tools（版本号最大目录）优先，PATH 兜底。"""
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ""
    if home:
        for name in ("aapt2", "aapt"):
            found = sorted(glob.glob(os.path.join(home, "build-tools", "*", name)))
            if found:
                return found[-1]
    return shutil.which("aapt2") or shutil.which("aapt") or ""


def _resolve_app_label(serial: str, package: str) -> str:
    aapt = _aapt_bin()
    if not aapt:
        return ""
    try:
        _, out = _run(_adb(serial, "shell", "pm", "path", package), timeout=15)
        apk = next((ln.split(":", 1)[1].strip() for ln in out.splitlines()
                    if ln.startswith("package:")), "")
        if not apk:
            return ""
        fd, tmp = tempfile.mkstemp(suffix=".apk")
        try:
            with os.fdopen(fd, "wb") as fh:
                subprocess.run(_adb(serial, "exec-out", "cat", apk),
                               stdout=fh, timeout=120, check=False)
            _, badging = _run([aapt, "dump", "badging", tmp], timeout=30)
            m = (re.search(r"^application-label:'([^']*)'", badging, re.MULTILINE)
                 or re.search(r"^application-label[^:]*:'([^']*)'", badging, re.MULTILINE))
            return m.group(1).strip() if m else ""
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except (OSError, subprocess.SubprocessError):
        return ""


def api_app_labels(root: Path, packages_raw: str) -> tuple[int, dict]:
    """GET /api/app/labels?packages=a,b：包名 → 显示名（有设备才解析，缓存加速）。"""
    wanted = [p.strip() for p in packages_raw.split(",") if p.strip()][:20]
    cache_path = root / "artifacts" / ".app_labels.json"
    try:
        labels: dict[str, str] = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        labels = {}
    missing = [p for p in wanted if not labels.get(p)]
    if missing:
        serials = [d["serial"] for d in list_adb_devices() if d.get("state") == "device"]
        for pkg in missing:
            if not serials:
                break
            label = _resolve_app_label(serials[0], pkg)
            if label:
                labels[pkg] = label
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(labels, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError:
            pass  # 缓存写失败只影响下次解析速度
    return 200, {"labels": {p: labels[p] for p in wanted if labels.get(p)}}
