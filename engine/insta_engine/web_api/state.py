"""/api/state、/api/doctor、/api/activate（环境与账号域）。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from .. import __version__, promptstore
from ..device import list_adb_devices
from ..quota import UsageStore
from ..readiness import EXPLORE_TIP
from .device import ios_state_devices
from .runtime import CONSOLE_DIST, _now, source_staleness
from .workspace import case_meta, list_explores, list_imports, list_indexes, list_runs


def api_state(root: Path, usage: dict | None = None) -> tuple[int, dict]:
    from .. import doctor
    doctor.reload_env(root)  # gen_ready 与环境检测同源（.env 改完页头 Tag 即刷新）
    cases_dir = root / "cases"
    # 用例列表不含套件（*.suite.json）——套件是「一次跑多条用例」的编排资产而非用例，
    # 走 /api/suites 单独管理（发起人 2026-10-11：用例套件无需显示在用例列表里）。
    # `_batch_*` 一并滤掉（2026-10-24 兜底）：--app 批量已改内存套件不落盘，但旧工作区
    # 可能残留历史文件，坏 JSON 曾让列表接口 500（评审附带项）。
    cases = (sorted(p.name for p in cases_dir.glob("*.json")
                    if not p.name.endswith(".suite.json") and not p.name.startswith("_batch_"))
             if cases_dir.exists() else [])
    # 使用方式页「套餐」行展示到期日（2026-10-13）：read_license 每次验签 ⇒ 续期（含时长卡
    # 核销换发的普通码）写盘后这里自然拿到新日期。plan 落回 free（未激活/已过期）时
    # expires_at 为 None，前端不显示日期。
    from ..licensing import read_license
    lic = read_license()
    return 200, {
        "version": __version__,
        "cwd": str(root),
        "cases": cases,
        "caseMeta": case_meta(root),
        "runs": list_runs(root),
        "imports": list_imports(root),
        "indexes": list_indexes(root),
        "explores": list_explores(root),
        "usage": usage or UsageStore().snapshot(),
        # AI 接入方式（2026-10-23）：server=官方代理（体验模式）/ local=自带 Key（标准模式）。
        # 前端据此显示模式标签，不再靠 gen_limit 是否为 null 猜档位。
        "ai_mode": "server" if promptstore.is_server() else "local",
        "license": {"plan": lic["plan"], "expires_at": lic.get("expires_at")},
        "code_stale": source_staleness(),
        # 与 doctor.gen / ai_client 校验同口径：server 模式需 KEY；本地模式需 KEY+GEN_MODEL+prompt
        "gen_ready": bool(os.environ.get("GEN_API_KEY"))
        and (promptstore.is_server()
             or (bool(os.environ.get("GEN_MODEL")) and promptstore.resolve("gen") is not None)),
        # 遍历探索建议（判据本体保留；2026-10-26 去掉「甜点判据」术语）：单一来源下发
        "explore_tip": EXPLORE_TIP,
        # Android(adb) + iOS USB 真机（2026-10-19：控制台真机入口；iOS 侧带 TTL 缓存）
        "devices": list_adb_devices() + ios_state_devices(),
        # iOS 工具链（Xcode / simctl / devicectl / xcodebuild）只在 macOS 存在：
        # Windows/Linux 前端据此隐藏 iOS 环境卡、iOS 运行平台与模拟器入口（2026-10-22）
        "ios_supported": sys.platform == "darwin",
        "console": "pro" if (CONSOLE_DIST / "index.html").exists() else "legacy",
        "prompt_mode": "server" if promptstore.is_server() else "local",
    }


def api_doctor(platform: str, root: Path) -> tuple[int, dict]:
    """环境自检（F2）：检查逻辑在 doctor.py，此处是配置台渲染层的数据出口。

    所有探测带超时（doctor 模块纪律）——配置台进页面自动检测，不能被挂死的探测拖住。
    每次检测前重载工作区 .env：长驻进程环境是启动时快照，不重载则用户改完配置
    点「重新检测」永远读到旧值（发起人 2026-10-12 实测踩中：删掉 GEN_MODEL 仍显示已配置）。
    """
    from .. import doctor
    doctor.reload_env(root)
    started = time.monotonic()
    checks = doctor.run(platform)
    return 200, {
        "checks": [c.as_dict() for c in checks],
        "ran_at": _now(),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def api_doctor_fix(host: str, root: Path, body: dict) -> tuple[int, dict]:
    """环境阻塞项一键修复（发起人 2026-10-11）：body 只带 action id，
    命令本体在 doctor.FIX_ACTIONS 白名单里，前端传不进任意命令；仅本机模式可用。"""
    from .. import doctor
    return doctor.run_fix(str(body.get("action") or ""), root, host)


def api_activate(body: dict) -> tuple[int, dict]:
    """Pro 激活码激活（控制台账号页入口）：验签 + 写 license.json，成功返回套餐信息。

    与 CLI `mat activate` 同一实现（licensing.activate）；失败返回 400 与原因。
    """
    code = str(body.get("code", "")).strip()
    if not code:
        return 400, {"error": "缺少激活码"}
    try:
        from ..licensing import activate
        info = activate(code)
    except Exception as exc:
        return 400, {"error": str(exc)}
    out = {"ok": True, "plan": info["plan"], "expires_at": info["expires_at"]}
    if info.get("redeemed"):
        out["note"] = "时长卡已在服务端核销，有效期以核销结果为准"
    elif info.get("offline_fallback"):
        out["note"] = str(info.get("note") or "")
    return 200, out
