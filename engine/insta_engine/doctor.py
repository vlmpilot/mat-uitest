"""环境自检:检查逻辑与展示分离(F2 环境监测可视化)。

`mat doctor`(CLI)与 `GET /api/doctor`(配置台)是**同一份检查的两个渲染层**——
此前检查逻辑内联在 `cmd_doctor` 的打印循环里,配置台无法复用。本模块只产出
结构化 `Check`,不打印;渲染职责在调用方(CLI 逐行打 ✅/❌,配置台渲染红黄绿卡片)。

status 语义(三层):
- "ok"   检查通过
- "warn" 可选能力缺失/未配置——主流程不受影响(iOS 整组、通知、视觉兜底均此类)
- "fail" 关键检查未通过,影响「能否开测」结论(与 CLI 退出码同源);
         显式覆盖用:如 wda-server 在 wda-lib 未装时降级为 warn(而非一片红)

纪律:
- 所有探测带超时(list_adb_devices 10s / WDA status 2s / simctl 3s)——配置台
  **进入页面自动检测**,任何一个探测挂死都会拖住整页。
- 对外数字必须能回答「测的是哪台设备」(T76 同款)。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .device import list_adb_devices

#: 「如何开启计费兜底」的**单一事实来源**(此前内联在 __main__,现随 doctor 逻辑
#: 迁入本模块;__main__ 反向 import 保持 CLI 文案引用不破)。
VISION_OPT_IN_HINT = "需步骤标 onFailure: heal 或设 GEN_VL_FALLBACK=1"

_WDA_STATUS_URL = "http://localhost:8100/status"
_WDA_TIMEOUT_S = 2
_SIMCTL_TIMEOUT_S = 3


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    critical: bool = False
    status: str = ""  # 留空 = 按 ok/critical 自动推断
    detail: str = ""
    hint: str = ""
    # 一键排除阻塞（发起人 2026-10-11：阻塞项在提示右侧直接给修复按钮）。
    # fix 只带 action id——命令本身在 FIX_ACTIONS 白名单里，前端传不进任意命令。
    fix: dict | None = None

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "ok" if self.ok else ("fail" if self.critical else "warn")

    def as_dict(self) -> dict:
        d = asdict(self)
        if self.fix:
            spec = FIX_ACTIONS.get(str(self.fix.get("action", "")))
            if spec:
                d["fix"] = {**self.fix, "kind": spec["kind"], "label": spec["label"]}
        return d


# -- 一键修复动作白名单（配置台 /api/doctor/fix 的唯一执行入口） --------------------
# kind=run：本机执行命令（列表参数、不经 shell）；kind=open：用系统默认程序打开文件
# （.env 不存在则以模板创建）。都只在本机模式下可用（--share 由网关层拒绝）。
FIX_ACTIONS: dict[str, dict] = {
    "install-adb": {"kind": "run", "label": "安装",
                    "cmd": ["brew", "install", "android-platform-tools"]},
    "install-u2": {"kind": "run", "label": "安装",
                   "cmd": [sys.executable, "-m", "pip", "install", "mat-uitest"]},
    "install-wda": {"kind": "run", "label": "安装",
                    "cmd": [sys.executable, "-m", "pip", "install", "mat-uitest[ios]"]},
    "install-scrcpy": {"kind": "run", "label": "安装",
                       "cmd": ["brew", "install", "scrcpy"]},
    "install-simctl": {"kind": "run", "label": "安装",
                       "cmd": ["xcode-select", "--install"]},
    "open-simulator": {"kind": "run", "label": "启动",
                       "cmd": ["open", "-a", "Simulator"]},
    # kind=wda：非单命令动作（launch + 轮询就绪），逻辑在 ios_wda.start_on_booted
    # （发起人 2026-10-12：WDA 未起只有 tips 没有入口，概览行上直接给一键启动）
    "start-wda": {"kind": "wda", "label": "启动"},
    "open-env": {"kind": "open", "label": "打开配置"},
}

ENV_TEMPLATE = (
    "# MAT 控制台环境变量（二选一配置 AI 生成）\n"
    "# 方式一：官方代理\n"
    "# GEN_BASE_URL=https://mobileaitest.com\n"
    "# GEN_API_KEY=<你的 token>\n"
    "# GEN_PROMPT_MODE=server\n"
    "# 方式二：自带 Key（OpenAI 兼容端点）\n"
    "# GEN_BASE_URL=<你的 OpenAI 兼容端点>\n"
    "# GEN_API_KEY=<你的 Key>\n"
    "# GEN_MODEL=<语言模型名>\n"
    "# GEN_VL_MODEL=<视觉模型名>\n"
)

_FIX_TIMEOUT_S = 600

# 环境检测关注的可配置项：重新检测前先清这组再从工作区 .env 重载——
# 否则长驻进程读的是启动时的旧环境，用户改完 .env 点「重新检测」永远是旧结论，
# 「打开配置→改→重新检测」闭环就是假的。配置台场景 .env 是权威来源（UI 引导编辑的就是它）。
_ENV_KEYS = ("GEN_BASE_URL", "GEN_API_KEY", "GEN_MODEL", "GEN_VL_MODEL",
             "GEN_PROMPT_MODE", "GEN_VL_FALLBACK", "MAT_WEBHOOK_URL")

_RELOADED: dict = {"mtime": None, "path": ""}  # 上次重载的 .env mtime（节流标记）


def reload_env(root: Path) -> None:
    """按 mtime 节流地重载工作区 .env：文件没变只做一次 stat（api_state 高频轮询
    调用时零 pop、零竞争窗口），变了才清 _ENV_KEYS 再加载。"""
    env_path = root / ".env"
    try:
        mtime = env_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _RELOADED.get("mtime") == mtime and _RELOADED.get("path") == str(env_path):
        return
    from .__main__ import _load_env_file

    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    _load_env_file(env_path)
    _RELOADED.update(mtime=mtime, path=str(env_path))


def run_fix(action: str, root: Path, host: str = "127.0.0.1") -> tuple[int, dict]:
    """执行白名单修复动作。run 返回 (exit, 输出尾部)；open 先建缺失的 .env 再打开。"""
    from .web_api.workspace import _local_only

    gate = _local_only(host)
    if gate:
        return gate
    spec = FIX_ACTIONS.get(action)
    if spec is None:
        return 400, {"error": f"未知修复动作: {action}"}
    if spec["kind"] == "wda":
        from .ios_wda import start_on_booted

        ok, detail = start_on_booted()
        wda_body: dict = {"ok": ok, "action": action, "detail": detail}
        if not ok:
            wda_body["error"] = detail  # j() 抛 Error(detail)，配置台 toast 直接可读
        return (200 if ok else 400), wda_body
    if spec["kind"] == "open":
        # 目前只有 .env——不存在则以模板创建，让「打开配置文件」永远有东西可改
        target = root / ".env"
        if not target.exists():
            target.write_text(ENV_TEMPLATE, encoding="utf-8")
        from .web_api.workspace import open_path_in_file_manager
        open_path_in_file_manager(str(target))
        return 200, {"ok": True, "action": action, "path": str(target)}
    try:
        proc = subprocess.run(spec["cmd"], capture_output=True, text=True,
                              timeout=_FIX_TIMEOUT_S)
    except FileNotFoundError:
        return 400, {"error": f"命令不存在: {spec['cmd'][0]}（未安装或不在 PATH）"}
    except subprocess.TimeoutExpired:
        return 504, {"error": f"执行超时（>{_FIX_TIMEOUT_S}s），可到终端手动执行:{' '.join(spec['cmd'])}"}
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    ok = proc.returncode == 0
    body: dict = {"ok": ok, "action": action, "exit": proc.returncode, "output": out[-2000:]}
    if not ok:
        # error 字段进 HTTP 语义（j() 抛 Error(body.error)），配置台 toast 直接可读
        body["error"] = f"退出码 {proc.returncode}: {out[-500:]}" if out else f"退出码 {proc.returncode}"
    return (200 if ok else 500), body


# -- Android 组(逻辑自 cmd_doctor 平移,文案不变) --------------------------------


def _prompt_is_template_copy(name: str, resolved: Path) -> bool:
    """工作区 <名>.prompt 内容与包内模板逐字节一致 ⇒ 视为「默认配置」。

    `mat init` 播种的是可编辑副本（三处一致性测试保证与包内同步），不该被显示成
    「工作区自定义」；逐字节比较同时能识别出真实改动过的文件。读不到包内模板
    （理论不发生）按「非默认」处理，宁可多说一句自定义也不漏报。
    """
    from . import promptstore

    try:
        return resolved.read_bytes() == (
            promptstore.PKG_PROMPTS / f"{name}.prompt.example").read_bytes()
    except OSError:
        return False


def android_checks() -> list[Check]:
    checks: list[Check] = []

    checks.append(Check(
        id="python",
        label=f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ok=sys.version_info >= (3, 11), critical=True,
        hint="需要 ≥3.11(推荐 python3.12)",
    ))

    adb_path = shutil.which("adb")
    checks.append(Check(
        id="adb", label=f"adb: {adb_path or '未找到'}",
        ok=bool(adb_path), critical=True,
        hint="安装:brew install android-platform-tools(或下载 platform-tools 加入 PATH)",
        fix={"action": "install-adb"} if not adb_path else None,
    ))

    devices = list_adb_devices()
    checks.append(Check(
        id="devices",
        label=f"在线设备: {len(devices)} 台" +
              (f"({', '.join(d.get('model', d['serial']) for d in devices[:2])})" if devices else ""),
        ok=bool(devices), critical=True,
        hint="手机开启 USB 调试并插上;WiFi 调试用 adb connect",
    ))

    checks.append(Check(
        id="u2", label="uiautomator2 驱动", ok=_u2_installed(), critical=True,
        hint="pip install -e './engine'(本仓库开发者)或 pip install mat-uitest",
        fix={"action": "install-u2"} if not _u2_installed() else None,
    ))

    # -- 提示级(不影响「能否开测」结论) ----------------------------------------
    # AI 卡收成两行（发起人 2026-10-14）：「AI生成prompt」+「AI模型配置」，
    # 各带常驻入口（管理→Prompt 管理浮层 / 配置→打开 .env）；视觉兜底保持第三行。
    from . import promptstore

    if promptstore.is_server():
        checks.append(Check(
            id="prompt", label="AI生成prompt: server下发", ok=True,
            hint="prompt 由服务端统一下发，本地 prompt 文件与「管理」里的选择暂不参与生效"
                 "（可以先选好，.env 把 GEN_PROMPT_MODE 改回 local 即生效）",
        ))
    else:
        parts: list[str] = []
        paths: list[str] = []
        missing: list[str] = []
        for name in ("gen", "convert"):
            kind, resolved = promptstore.describe_resolution(name)
            if resolved is None:
                missing.append(name)
                parts.append(f"{name} 未找到")
                continue
            paths.append(f"{name}: {resolved}")
            if kind == "chosen":
                parts.append(f"{name} 已选择 {resolved.name}")
            elif kind == "workspace" and not _prompt_is_template_copy(name, resolved):
                parts.append(f"{name} 工作区自定义")
            # 其余（包内模板/工作区模板/播种后未改动的副本）＝ 默认配置，不单列
        checks.append(Check(
            id="prompt",
            label="AI生成prompt: " + (" · ".join(parts) if parts else "默认配置"),
            ok=not missing,
            detail="\n".join(paths),
            hint=("点「管理」可选择/新建/编辑 prompt 文件（存工作区 prompts/，改完即生效无需重启）"
                  + (f"；{'、'.join(missing)} 未找到，运行 `mat init` 播种模板" if missing else "")),
        ))

    server_mode = promptstore.is_server()
    # 提示文案按模式分类（发起人 2026-10-14：默认按官方代理引导，配了 local 才给 local 引导）。
    # 「local 倾向」= 显式 GEN_PROMPT_MODE=local，或没写 mode 但已配 GEN_MODEL（自带 Key 用户）；
    # 其余（显式 server、或什么都没配的新用户）都按官方代理引导——官方代理配置项最少。
    local_inclined = (promptstore.mode() == "local"
                      or (not promptstore.is_server() and os.environ.get("GEN_MODEL")))
    if local_inclined:
        gen_hint = (
            "当前：自带 Key（本地组装 prompt）\n"
            "GEN_BASE_URL=<你的 OpenAI 兼容端点>\n"
            "GEN_API_KEY=<你的 Key>\n"
            "GEN_MODEL=<语言模型名>\n"
            "GEN_VL_MODEL=<视觉模型名>\n"
            "prompt 可在「AI生成prompt」行「管理」里选择/自定义（server 模式则由服务端下发）\n"
            "切官方代理（.env 改三行）：\n"
            "GEN_BASE_URL=https://mobileaitest.com\n"
            "GEN_API_KEY=<你的 token>\n"
            "GEN_PROMPT_MODE=server"
        )
    else:
        gen_hint = (
            "方式一：官方代理（推荐，配置最少）\n"
            "GEN_BASE_URL=https://mobileaitest.com\n"
            "GEN_API_KEY=<你的 token>\n"
            "GEN_PROMPT_MODE=server\n"
            "（模型与 prompt 均由服务端管理，无需 GEN_MODEL/GEN_VL_MODEL）\n"
            "方式二：自带 Key（OpenAI 兼容端点）\n"
            "GEN_BASE_URL=<你的 OpenAI 兼容端点>\n"
            "GEN_API_KEY=<你的 Key>\n"
            "GEN_MODEL=<语言模型名>\n"
            "GEN_VL_MODEL=<视觉模型名>"
        )
    gen_ok = bool(os.environ.get("GEN_API_KEY")) and (server_mode or bool(os.environ.get("GEN_MODEL")))
    # detail 说清「还缺哪一项」——只报「未配置」会让人对着一排 GEN_* 猜
    gen_missing = "" if gen_ok else (
        "缺少 GEN_API_KEY" if not os.environ.get("GEN_API_KEY") else
        "方式一需 GEN_PROMPT_MODE=server；方式二需 GEN_MODEL（自带 Key 模式）")
    # 状态文案按生效模式分（发起人 2026-10-14）：server 显「server模式 · 已配置」；
    # 配了自部署（GEN_MODEL）显「已优先使用自部署」——对齐「配了 local 就取 local」口径。
    gen_state = ("server模式 · 已配置" if server_mode else "已优先使用自部署"
                 if gen_ok else "未配置(建议配置)")
    # 「建议配置」≠「可选」（发起人 2026-10-12）：AI 生成是主路径能力，缺失会明显
    # 降低易用性，但缺失不阻塞手动用例执行——仍是提示级 warn，不计入退出码。
    # 「配置」按钮由前端常驻渲染（fix 不再用：open-env 在未配置时也要能进）。
    checks.append(Check(
        id="gen", label=f"AI模型配置({gen_state})", ok=gen_ok, hint=gen_hint, detail=gen_missing,
    ))

    # 视觉兜底三态（对齐 CLI vision_status_text 的核心提醒：「配了模型」≠「会触发」）：
    # 未配置=warn 引导配置；已配置未授权=warn 提示两条授权路径（不擅自断定该改哪边——
    # 步骤级 onFailure: heal 是改用例不是改配置）；全局授权=ok。模型名不上 UI（发起人 2026-10-12）。
    # 提示按模式分类（发起人 2026-10-14）：官方代理模式下视觉模型由服务端决定，
    # 本地不需要 GEN_VL_MODEL——照抄「加 GEN_VL_MODEL」配了也不生效，必须分开说。
    vl = os.environ.get("GEN_VL_MODEL")
    vl_usable = gen_ok if server_mode else bool(vl and gen_ok)
    vl_authorized = os.environ.get("GEN_VL_FALLBACK") == "1"
    if not vl_usable:
        vl_label = "视觉兜底(未配置)"
        vl_hint = (f"官方代理模式下无需 GEN_VL_MODEL：完成上方 AI 模型配置(方式一)后,"
                   f"设 GEN_VL_FALLBACK=1 即启用;自带 Key 模式:.env 加 GEN_VL_MODEL 开启;"
                   f"开启后仍需显式授权才触发——{VISION_OPT_IN_HINT}"
                   if not local_inclined else
                   f".env 加 GEN_VL_MODEL 开启;开启后仍需显式授权才触发——{VISION_OPT_IN_HINT}")
    elif vl_authorized:
        vl_label = "视觉兜底(已启用,全局授权 GEN_VL_FALLBACK=1)"
        vl_hint = "定位失败自动走截图定位"
    else:
        vl_label = "视觉兜底(已配置,默认不触发)"
        vl_hint = (f"触发需显式授权:步骤标 onFailure: heal(逐步骤),"
                   f"或 .env 设 GEN_VL_FALLBACK=1(全局)——{VISION_OPT_IN_HINT}")
    checks.append(Check(
        id="vl", label=f"{vl_label}(建议配置)", ok=vl_usable and vl_authorized, hint=vl_hint,
        fix={"action": "open-env"} if not (vl_usable and vl_authorized) else None,
    ))

    webhook = bool(os.environ.get("MAT_WEBHOOK_URL"))
    checks.append(Check(
        id="notify", label="结果通知(MAT_WEBHOOK_URL)(可选)",
        ok=webhook,
        hint="可选:在运行目录 .env 加 MAT_WEBHOOK_URL(飞书/钉钉机器人地址),见手册 §4.9",
        fix={"action": "open-env"} if not webhook else None,
    ))

    # 套餐检查项已移除（发起人 2026-10-13）：概览 AI 生成卡底部的「套餐 标准版 · 本月生成
    # 159.5/不限」与环境就绪检测主题无关，额度概览统计卡已有展示，无需重复

    # scrcpy 独立投屏检测暂时下架（发起人 2026-10-12）：可选能力易被误读为缺失告警；
    # 浏览器内投屏不依赖它。ACTIONS 里的 install-scrcpy 保留，需要时再恢复卡片。
    return checks


# -- iOS 组(可选能力:缺失不影响 Android,不计入退出码) ---------------------------


def _u2_installed() -> bool:
    try:
        import uiautomator2  # noqa: F401
        return True
    except ImportError:
        return False


def _wda_installed() -> bool:
    try:
        import wda  # noqa: F401
        return True
    except ImportError:
        return False


def _wda_status_ok() -> bool:
    try:
        with urllib.request.urlopen(_WDA_STATUS_URL, timeout=_WDA_TIMEOUT_S) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 —— 探测失败即「不可达」,细节不进入结论
        return False


def _booted_simulators() -> list[str]:
    try:
        out = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "booted"],
            capture_output=True, text=True, timeout=_SIMCTL_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if "(Booted)" in line]


def ios_checks() -> list[Check]:
    checks: list[Check] = []

    wda_ok = _wda_installed()
    checks.append(Check(
        id="wda-lib", label="facebook-wda 驱动", ok=wda_ok,
        detail="" if wda_ok else "未安装(可选)",
        hint="pip install 'mat-uitest[ios]'(iOS 模拟器支持)",
        fix={"action": "install-wda"} if not wda_ok else None,
    ))

    simctl_ok = bool(shutil.which("xcrun"))
    checks.append(Check(
        id="simctl", label="Xcode 命令行工具(xcrun simctl)", ok=simctl_ok,
        hint="brew install --cask xcodes-app,或仅装 Command Line Tools:xcode-select --install",
        fix={"action": "install-simctl"} if not simctl_ok else None,
    ))

    booted = _booted_simulators()
    checks.append(Check(
        id="simulators", label=f"已启动模拟器: {len(booted)} 台" +
                               (f"({booted[0].split('(')[0].strip()})" if booted else ""),
        ok=bool(booted),
        hint="打开 Simulator.app 选机型启动;iOS 用例执行前置",
        fix={"action": "open-simulator"} if not booted else None,
    ))

    server_ok = _wda_status_ok()
    checks.append(Check(
        id="wda-server", label="WDA 服务 (localhost:8100)", ok=server_ok,
        # 装了库但服务没起 = 想用 iOS 而当前不可用(fail);库都没装 = 用户没进 iOS
        # 这条赛道(warn 降级)——main 分支默认呈现「未安装(可选)」的诚实态,不是一片红。
        status=("warn" if not wda_ok else ("ok" if server_ok else "fail")),
        hint="模拟器/真机运行 WDA 后可用(复现命令见仓库)",
        # 一键启动（发起人 2026-10-12）：模拟器上装过 WDA 的话 simctl launch 免构建
        fix={"action": "start-wda"} if (wda_ok and not server_ok) else None,
    ))

    # usbmux/pymobiledevice3 检查项已隐藏（发起人 2026-10-12）：真机转发是 P2 规划
    # 能力、当前 iOS 真机链路尚未支持，展示一条永远 warn 的检查项只会制造噪音。
    # 等真机转发落地时再随功能一起恢复。

    return checks


def run(platform: str = "all") -> list[Check]:
    """聚合检查。platform ∈ {"android", "ios", "all"}。"""
    if platform == "android":
        return android_checks()
    if platform == "ios":
        return ios_checks()
    return android_checks() + ios_checks()
