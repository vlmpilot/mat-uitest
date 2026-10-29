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


def _local_endpoint_down(endpoint: str) -> bool | None:
    """本机端点**没在监听**？True=连不上 / False=能连 / None=不在本机或无法解析。

    只给诊断提示用，不参与执行：超时 0.3s，宁可判"未知"也不拖住配置台整页检测
    （纪律见模块头：进页面自动检测，任何探测挂死都会拖住整页）。
    """
    from urllib.parse import urlsplit

    from .offline import is_loopback

    if not is_loopback(endpoint):
        return None
    try:
        parts = urlsplit(endpoint if "://" in endpoint else "//" + endpoint)
        host = (parts.hostname or "").strip("[]")
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    if not host:
        return None
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.3):
            return False
    except OSError:
        return True


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
    # kind=open-sim：Simulator.app 在 Xcode 包内、`open -a` 常找不到（用户实测）——
    # 逻辑在 ios_setup.open_simulator_app（按 xcode-select 拼确定性路径）
    "open-simulator": {"kind": "open-sim", "label": "启动"},
    # kind=wda：非单命令动作（launch + 轮询就绪），逻辑在 ios_wda.start_on_booted
    # （发起人 2026-10-12：WDA 未起只有 tips 没有入口，概览行上直接给一键启动）
    "start-wda": {"kind": "wda", "label": "启动"},
    # 真机 WDA：start-wda 只认模拟器（simctl），真机必须走 usbmux + xcodebuild
    "start-wda-device": {"kind": "wda-device", "label": "启动真机 WDA"},
    # 证书续期（= 重新签名；xcodebuild 能用账号才有效，否则如实回滚并提示 ⌘U）
    "renew-cert": {"kind": "renew", "label": "续期证书"},
    "open-env": {"kind": "open", "label": "打开配置"},
    # 登录 Apple ID / 信任证书都必须在 Xcode GUI 里做——**唯一能自动的是把 Xcode 打开**，
    # 剩下的文字引导写在该检查项的 hint 里（不要假装能一键登录）。
    "open-xcode": {"kind": "run", "label": "打开 Xcode",
                   "cmd": ["open", "-a", "Xcode"]},
}

ENV_TEMPLATE = (
    "# MAT 控制台环境变量（二选一配置 AI 生成）\n"
    "# 方式一（推荐）：自带 Key（OpenAI 兼容端点）——prompt 本地拼装、数据不经我们，模型可自选\n"
    "# GEN_BASE_URL=<你的 OpenAI 兼容端点>\n"
    "# GEN_API_KEY=<你的 Key>\n"
    "# GEN_MODEL=<语言模型名>\n"
    "# GEN_VL_MODEL=<视觉模型名>\n"
    "# GEN_VL_BASE_URL=<视觉端点>（可选：兜底单独走一个端点，如本机小模型）\n"
    "# MAT_VL_MAX_EDGE=1280（可选：送模型前压缩截图长边，默认不缩）\n"
    "# 方式二：官方代理（零配置试用；关注公众号「Mobile AI Test」获取代理token）\n"
    "GEN_BASE_URL=https://mobileaitest.com\n"
    "GEN_API_KEY=<平台代理token>\n"
    "GEN_PROMPT_MODE=server\n"
    "# 视觉兜底的总开关\n"
    "GEN_VL_FALLBACK=0\n"
)

_FIX_TIMEOUT_S = 600

# 环境检测关注的可配置项：重新检测前先清这组再从工作区 .env 重载——
# 否则长驻进程读的是启动时的旧环境，用户改完 .env 点「重新检测」永远是旧结论，
# 「打开配置→改→重新检测」闭环就是假的。配置台场景 .env 是权威来源（UI 引导编辑的就是它）。
# ⚠️ 新增任何「引擎会读」的配置项都必须登记到这里，否则配置台改了不生效——
# `GEN_VL_BASE_URL` / `MAT_VL_MAX_EDGE` 就是漏在这里过（2026-10-26 补）。
_ENV_KEYS = ("GEN_BASE_URL", "GEN_API_KEY", "GEN_MODEL", "GEN_VL_MODEL",
             "GEN_VL_BASE_URL", "MAT_VL_MAX_EDGE",
             "GEN_PROMPT_MODE", "GEN_VL_FALLBACK", "MAT_WEBHOOK_URL",
             "GEN_DISABLE_THINKING")

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


def _usb_device_udid() -> str | None:
    """当前 USB 连接的真机 udid（没有则 None）。**失败不抛**，配置台不能被它拖住。"""
    try:
        from . import ios_device

        for d in ios_device.list_devices():
            if d.get("transport") == "wired":
                return d["udid"]
    except Exception:  # noqa: BLE001 —— 探测失败按"没有"处理
        return None
    return None


def run_fix(action: str, root: Path, host: str = "127.0.0.1") -> tuple[int, dict]:
    """执行白名单修复动作。run 返回 (exit, 输出尾部)；open 先建缺失的 .env 再打开。"""
    from .web_api.workspace import _local_only

    gate = _local_only(host)
    if gate:
        return gate
    spec = FIX_ACTIONS.get(action)
    if spec is None:
        return 400, {"error": f"未知修复动作: {action}"}
    if spec["kind"] == "wda-device":
        from .ios_device import list_devices, pick_device
        from .ios_wda import start_on_device

        usb = [d for d in list_devices() if d.get("transport") == "wired"]
        dev, _why = pick_device(devices=usb)
        if dev is None:
            return 400, {"error": "没有 USB 连接的 iOS 真机——请用数据线连接并解锁后重试"}
        ok, detail = start_on_device(dev["udid"])
        body: dict = {"ok": ok, "action": action, "detail": detail}
        if not ok:
            body["error"] = detail
        return (200 if ok else 400), body
    if spec["kind"] == "renew":
        from .ios_device import list_devices, pick_device
        from .ios_wda import renew_device_profile

        # 续期 = 重新签名，必须有可用 USB 真机作 build 目标；Wi-Fi 设备试了也白试
        usb = [d for d in list_devices() if d.get("transport") == "wired"]
        dev, _why = pick_device(devices=usb)
        if dev is None:
            return 400, {"error": "续期证书需要先用数据线（USB）连接 iPhone 并解锁；"
                                  "续期成功后需重启 WDA 才生效"}
        ok, detail = renew_device_profile(dev["udid"])
        body = {"ok": ok, "action": action, "detail": detail}
        if not ok:
            body["error"] = detail
        return (200 if ok else 400), body
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
    if spec["kind"] == "open-sim":
        from .ios_setup import open_simulator_app

        ok, detail = open_simulator_app()
        body: dict = {"ok": ok, "action": action, "detail": detail}
        if not ok:
            body["error"] = detail
        return (200 if ok else 400), body
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
    # 提示文案按模式分类。2026-10-21 起**推荐 BYOK**（数据不经我们），但 .env 模板里
    # 默认生效的仍是官方代理——新用户不配 Key 也能先跑通，避免首启即"AI 未配置"。
    local_inclined = (promptstore.mode() == "local"
                      or (not promptstore.is_server() and os.environ.get("GEN_MODEL")))
    if local_inclined:
        gen_hint = (
            "当前：自带 Key（本地组装 prompt，数据不经我们）\n"
            "GEN_BASE_URL=<你的 OpenAI 兼容端点>\n"
            "GEN_API_KEY=<你的 Key>\n"
            "GEN_MODEL=<语言模型名>\n"
            "GEN_VL_MODEL=<视觉模型名>（可选）\n"
            "切官方代理（零配置试用，.env 改三行）：\n"
            "GEN_BASE_URL=https://mobileaitest.com\n"
            "GEN_API_KEY=<平台代理token>\n"
            "GEN_PROMPT_MODE=server"
        )
    else:
        gen_hint = (
            "方式一（推荐）：自带 Key（OpenAI 兼容端点）——数据不经我们\n"
            "GEN_BASE_URL=<你的 OpenAI 兼容端点>\n"
            "GEN_API_KEY=<你的 Key>\n"
            "GEN_MODEL=<语言模型名>\n"
            "GEN_VL_MODEL=<视觉模型名>（可选）\n"
            "方式二：官方代理（零配置试用；关注公众号「Mobile AI Test」获取代理token）\n"
            "GEN_BASE_URL=https://mobileaitest.com\n"
            "GEN_API_KEY=<平台代理token>\n"
            "GEN_PROMPT_MODE=server\n"
            "（模型与 prompt 均由服务端管理，无需 GEN_MODEL/GEN_VL_MODEL）"
        )
    gen_ok = bool(os.environ.get("GEN_API_KEY")) and (server_mode or bool(os.environ.get("GEN_MODEL")))
    # detail 说清「还缺哪一项」——只报「未配置」会让人对着一排 GEN_* 猜
    gen_missing = "" if gen_ok else (
        "缺少 GEN_API_KEY" if not os.environ.get("GEN_API_KEY") else
        "自带 Key 模式需 GEN_MODEL；官方代理模式需 GEN_PROMPT_MODE=server")
    # 状态文案按生效方式分（发起人 2026-10-14；2026-10-25 起「模式」一词专指授权档
    # 体验/标准/团队，AI 接入维度不再占用它）：server 显「server · 已配置」；
    # 配了自部署（GEN_MODEL）显「已优先使用自部署」——对齐「配了 local 就取 local」口径。
    gen_state = ("server · 已配置" if server_mode else "已优先使用自部署"
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
    from .offline import is_loopback
    from .vision import endpoint

    vl = os.environ.get("GEN_VL_MODEL")
    # 端点在本机时模型名必须由本地给（本机服务不会替我们选模型）——对齐
    # `vision.enabled()`；官方代理的「模型服务端决定」在本机端点上不成立。
    vl_ep = endpoint()
    vl_local = is_loopback(vl_ep)
    vl_usable = bool(vl and gen_ok) if (vl_local or not server_mode) else gen_ok
    vl_authorized = os.environ.get("GEN_VL_FALLBACK") == "1"
    # 「已启用」只说明配置齐了。端点在本机而服务没起时，每次定位失败都是 ConnectionError
    # （报告里写「vision-llm 调用异常」），人却在查额度与授权——探一次 TCP 直接说破。
    vl_local_down = _local_endpoint_down(vl_ep) is True
    vl_ok = vl_usable and vl_authorized
    if not vl_usable:
        vl_label = "视觉兜底(未配置)"
        vl_hint = (f"官方代理模式下无需 GEN_VL_MODEL：完成上方 AI 模型配置(方式一)后,"
                   f"设 GEN_VL_FALLBACK=1 即启用;自带 Key 模式:.env 加 GEN_VL_MODEL 开启;"
                   f"开启后仍需显式授权才触发——{VISION_OPT_IN_HINT}"
                   if not local_inclined else
                   f".env 加 GEN_VL_MODEL 开启;开启后仍需显式授权才触发——{VISION_OPT_IN_HINT}")
    elif vl_authorized and vl_local_down:
        vl_label = "视觉兜底(已启用,本机端点未监听)"
        vl_hint = (f"本机视觉服务没在 {vl_ep} 上监听：先启动它，否则每次定位失败都会"
                   f"ConnectionError（报告显示「vision-llm 调用异常」）")
        vl_ok = False
    elif vl_authorized:
        vl_label = "视觉兜底(已启用,全局授权 GEN_VL_FALLBACK=1)"
        vl_hint = "定位失败自动走截图定位"
    else:
        vl_label = "视觉兜底(已配置,默认不触发)"
        vl_hint = (f"触发需显式授权:步骤标 onFailure: heal(逐步骤),"
                   f"或 .env 设 GEN_VL_FALLBACK=1(全局)——{VISION_OPT_IN_HINT}")
    checks.append(Check(
        id="vl", label=f"{vl_label}(建议配置)", ok=vl_ok, hint=vl_hint,
        fix={"action": "open-env"} if not vl_ok else None,
    ))

    webhook = bool(os.environ.get("MAT_WEBHOOK_URL"))
    checks.append(Check(
        id="notify", label="结果通知(MAT_WEBHOOK_URL)(可选)",
        ok=webhook,
        hint="可选:在运行目录 .env 加 MAT_WEBHOOK_URL(飞书/钉钉机器人地址),见使用手册「结果通知」",
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
    """WDA 服务就绪判据：模拟器走 localhost:8100，USB 真机走 usbmux。

    真机 WDA **不在 localhost**（USBClient 走 /var/run/usbmuxd），旧判据只探
    localhost ⇒ 真机屏幕已显示 Automation Running 仍报「WDA 服务未起」
    （2026-10-22 发起人实测）。先探模拟器，再探当前唯一 USB 真机。
    """
    try:
        with urllib.request.urlopen(_WDA_STATUS_URL, timeout=_WDA_TIMEOUT_S) as resp:
            if resp.status == 200:
                return True
    except Exception:  # noqa: BLE001 —— 探测失败继续试真机
        pass
    try:
        import wda  # noqa: F401 —— 没装驱动时不必再起 devicectl 子进程
    except ImportError:
        return False
    udid = _usb_device_udid()
    if not udid:
        return False
    from .ios_wda import device_wda_ready

    return device_wda_ready(udid)


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
    """iOS 组的检查结果——**任何平台都返回同一套结构**（2026-10-25 修正）。

    原实现非 macOS 直接 `return []`，于是「这一组有没有内容」成了平台相关的隐式契约：
    CLI 靠 `sys.platform == "darwin"` 不渲染（对），但**同一份检查在测试里被直接调用**
    ⇒ Linux/Windows CI 上 10 条结构断言全 KeyError（ci 工作流连红 7 天的一个来源）。
    契约改成「检查恒有结构，**渲染由调用方决定**」：CLI 决定不显示，控制台仍能看到
    「本机没有这套工具链」的诚实结论，而不是一片空白（空白无法区分「没检查」和
    「不支持」）。

    非 macOS 上探测照跑，但它们都是**失败即返回**的（xcrun/devicectl 不存在 → OSError；
    localhost:8100 直接拒连），不会像 macOS 上那样等超时。差别只在两处：平台专属的修复
    按钮一律收起（install-simctl / open-simulator / start-wda / open-xcode 在 Windows 上
    是空话），并用 `detail` 说明「仅 macOS 提供」。状态语义保持原口径：装了 wda 库但服务
    没起 = fail（"想用 iOS 而当前不可用"），跟 test_wda_server_fails_when_lib_present
    钉的契约一致。
    """
    supported = sys.platform == "darwin"
    platform_note = "" if supported else f"iOS 工具链仅在 macOS 提供（当前 {sys.platform}）"
    checks: list[Check] = []

    wda_ok = _wda_installed()
    # 只探一次：真机探测含 devicectl 子进程，多处复用避免环境卡越检测越慢
    server_ok = _wda_status_ok()
    checks.append(Check(
        id="wda-lib", label="facebook-wda 驱动", ok=wda_ok,
        detail="" if wda_ok else "未安装(可选)",
        hint="pip install 'mat-uitest[ios]'(iOS 模拟器+真机支持)",
        fix={"action": "install-wda"} if not wda_ok else None,
    ))

    simctl_ok = bool(shutil.which("xcrun"))
    checks.append(Check(
        id="simctl", label="Xcode 命令行工具(xcrun simctl)", ok=simctl_ok,
        detail=platform_note,  # 非 macOS 由 detail 说明「为什么不可用」，hint 保持原动作
        hint="brew install --cask xcodes-app,或仅装 Command Line Tools:xcode-select --install",
        # 非 macOS 上「去装 Xcode」这个修复动作毫无意义 ⇒ 不给按钮（给了就是误导）
        fix={"action": "install-simctl"} if (not simctl_ok and supported) else None,
    ))

    booted = _booted_simulators()
    checks.append(Check(
        id="simulators", label=f"已启动模拟器: {len(booted)} 台" +
                               (f"({booted[0].split('(')[0].strip()})" if booted else ""),
        ok=bool(booted),
        # 真机与模拟器任一可用即可（2026-10-22）：有 USB 真机时模拟器不是前置条件
        hint="打开 Simulator.app 选机型启动；已连接 USB 真机时可忽略",
        fix={"action": "open-simulator"} if (not booted and supported) else None,
    ))

    # 真机（2026-10-17 P2）：**只在装了 [ios] extra 时出现**——纯 Android 用户看不到这两行，
    # 避免"永远 warn 的噪音"（与 usbmux 检查此前被隐藏同一理由）。
    if wda_ok:
        from . import ios_device

        # 只认 USB（2026-10-22）：devicectl 会把 Wi-Fi 无线调试的设备也列出来，
        # 那些设备能配对但不能跑 WDA——环境卡干脆不展示，避免用户误以为可用。
        devices = ios_device.list_devices()
        usb = [d for d in devices if d.get("transport") == "wired"]
        if usb:
            checks.append(Check(
                id="ios-device",
                label=(f"真机: {len(usb)} 台 USB"
                       f"（{ios_device.describe(usb[0], with_transport=False)}）"),
                ok=True,
            ))
        else:
            checks.append(Check(
                id="ios-device", label="真机: 0 台", ok=False, status="warn",
                detail=platform_note,
                hint="用数据线（USB）连接 iPhone 并解锁（只用模拟器可忽略）",
            ))
        team = ios_device.team_id()
        checks.append(Check(
            id="ios-team", label=f"签名团队: {team or '未找到'}", ok=bool(team),
            status="ok" if team else "warn",
            detail=platform_note,
            hint="" if team else "真机签名需要 Xcode 登录 Apple ID（Settings → Accounts）",
        ))
        # Xcode 账号必须**读磁盘**（2026-10-17 实测：cfprefsd 有、磁盘没有 ⇒ xcodebuild 报
        # No accounts）。这条检查就是为了让用户不必靠猜。
        accounts = ios_device.xcode_account_count()
        from . import ios_setup as _ios_setup

        has_signed = _ios_setup.signed_runner_exists()
        if accounts:
            acc_label, acc_hint = f"Xcode 账号: {accounts} 个", ""
        elif has_signed:
            # 有已签名产物 ⇒ 账号不是阻塞项（复用路径不用账号），别让人去登一个不需要的号
            acc_label, acc_hint = "Xcode 账号: 未登录（可跳过——已有已签名产物）", \
                "只有需要重新构建 WDA 时才要登录账号"
        else:
            acc_label, acc_hint = "Xcode 账号: 未登录", \
                "Xcode → Settings → Accounts 登录 Apple ID；登录后退出 Xcode 让偏好落盘"
        checks.append(Check(
            id="xcode-account", label=acc_label, ok=bool(accounts) or has_signed,
            status="ok" if (accounts or has_signed) else "warn", hint=acc_hint,
            # 只有"真阻塞"（没账号且没已签名产物）才给入口——可跳过时给按钮是误导；
            # 非 macOS 根本没有 Xcode 可打开，给按钮同样是误导（2026-10-25）
            fix=None if (accounts or has_signed or not supported) else {"action": "open-xcode"},
        ))
        # 真机三项（与 `mat ios-setup` **同一实现**，不在这里重写一份）：
        # 证书有效期 / 设备解锁 / 已签名产物。控制台此前看不到这三项，
        # 而它们恰好是真机跑测最常卡住的地方（2026-10-20）。
        from . import ios_setup

        # 证书有效期 / 已签名产物与「插没插线」无关：有已配对真机（含 Wi-Fi）就展示；
        # 「设备解锁」必须有实机 udid，只在 USB 时加（2026-10-22 发起人反馈：
        # 上一版把三项一起藏了，证书有效期检测消失）。
        if devices:
            extras = (ios_setup.cert_check(),
                      ios_setup.lock_check(usb[0]["udid"]) if usb else None,
                      ios_setup.signed_runner_check(ios_setup.signed_runner_exists(),
                                                    server_ok))
            for extra in extras:
                if extra is not None:
                    checks.append(extra)

    checks.append(Check(
        # 标签不再写死 localhost（真机 WDA 走 usbmux，localhost 探不到）
        id="wda-server",
        label="WDA 服务 (模拟器 / USB 真机)" + ("" if supported else "（需 macOS）"),
        ok=server_ok,
        # 装了库但服务没起 = 想用 iOS 而当前不可用(fail);库都没装 = 用户没进 iOS
        # 这条赛道(warn 降级)——main 分支默认呈现「未安装(可选)」的诚实态,不是一片红。
        status=("warn" if not wda_ok else ("ok" if server_ok else "fail")),
        detail=platform_note,
        hint="模拟器/真机运行 WDA 后可用(复现命令见仓库)",
        # 一键启动（发起人 2026-10-12）：模拟器上装过 WDA 的话 simctl launch 免构建
        # 有 USB 真机 ⇒ 走真机启动（start-wda 只认模拟器，点它没用）；否则模拟器
        # 非 macOS 没有可点的修复动作（simctl/devicectl 都不存在）⇒ 一律不给按钮
        fix=({"action": "start-wda-device"}
             if (supported and wda_ok and not server_ok and _usb_device_udid())
             else ({"action": "start-wda"} if (supported and wda_ok and not server_ok) else None)),
    ))

    # usbmux/pymobiledevice3 检查项不在此处展示（2026-10-19 更新）：真机链路已打通
    # （usbmux 由 wda.USBClient 内建；syslog 证据源的可用性在 `mat ios-setup` 的
    # evidence 检查项里呈现），doctor 保持精简不重复。

    return checks


def harmony_checks() -> list[Check]:
    """鸿蒙环境检查（2026-10-26）：hdc 可用性 + 在线设备。

    鸿蒙链路整体是**可选能力**——hdc 缺失只是 warn 不是 critical（别让只用
    Android/iOS 的用户看到满屏红）。检查失败给可行动指引（DevEco 位置/MAT_HDC）。
    """
    from .harmony_hdc import hdc_path, list_targets

    checks: list[Check] = []
    binpath = hdc_path()
    checks.append(Check(
        id="hdc", label=f"hdc: {binpath or '未找到'}", ok=bool(binpath),
        detail="" if binpath else "未安装(可选)",
        hint="随 DevEco Studio 分发(macOS 标准位置已自动发现);非标准安装可设 MAT_HDC 指向 hdc",
    ))
    targets: list[str] = []
    if binpath:
        try:
            targets = list_targets()
        except Exception:  # noqa: BLE001 —— hdc 存在但 daemon 异常,按无设备
            targets = []
    checks.append(Check(
        id="harmony-devices", label=f"鸿蒙设备/模拟器: {len(targets)} 台", ok=bool(targets),
        detail="、".join(targets[:3]) if targets else "未连接(可选)",
        hint="DevEco Studio → Tools → Device Manager 创建并启动模拟器;或 USB 连接鸿蒙 NEXT 真机",
    ))
    return checks


def run(platform: str = "all") -> list[Check]:
    """聚合检查。platform ∈ {"android", "ios", "harmony", "all"}。"""
    if platform == "android":
        return android_checks()
    if platform == "ios":
        return ios_checks()
    if platform == "harmony":
        return harmony_checks()
    return android_checks() + ios_checks() + harmony_checks()
