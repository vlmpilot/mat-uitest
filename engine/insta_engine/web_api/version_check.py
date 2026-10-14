"""新版本检查（发起人 2026-10-14：控制台左上角版本号 + 新版提示）。

设计边界：
- 仅在控制台服务启动时后台查**一次** PyPI JSON API（1.5s 超时，失败静默——
  断网/超时/解析失败都当作「查不到」，绝不阻塞服务启动、绝不打扰使用）；
- 请求不带任何机器信息（无指纹/无激活码），与 `mat activate` 的出网完全分开；
- `MAT_NO_UPDATE_CHECK=1` 可整体关闭（隐私敏感用户）；
- 「点击更新」不做原地自动替换：运行中的进程无法可靠替换自己（pipx/uv/pip
  各有安装位置，升级是包管理器的事），弹窗按安装渠道给对应升级命令，
  用户升级后重启控制台生效。
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
from datetime import UTC, datetime
from urllib.request import Request, urlopen

from .. import __version__

_PYPI_URL = "https://pypi.org/pypi/mat-uitest/json"
_TIMEOUT = 1.5  # 秒；查不到就算了，体验优先

_lock = threading.Lock()
_result: dict | None = None  # {"latest": "0.6.2", "checked_at": iso}；None = 未完成/失败/未启用


def _ver_tuple(version: str) -> tuple[int, ...] | None:
    """'0.6.1rc1' → (0, 6, 1)（段内非数字尾巴截掉，rc/后缀不参与比较）；解析失败返回 None。"""
    try:
        return tuple(int(re.sub(r"\D.*$", "", seg) or 0) for seg in version.strip().lstrip("v").split("."))
    except ValueError:
        return None


def _upgrade_command() -> str:
    """按安装渠道给对应升级命令——提示里不能给一条跑不通的命令（装的是 pipx 却让 pip upgrade）。"""
    prefix = sys.prefix
    if "pipx" in prefix:
        return "pipx upgrade mat-uitest"
    if "uv" in prefix and "tools" in prefix:
        return "uv tool upgrade mat-uitest"
    exe = "py -m pip" if platform.system() == "Windows" else "pip"
    return f"{exe} install --upgrade mat-uitest"


def _check_once() -> str:
    """查询 PyPI 最新版本号；任何网络/解析异常向上抛（调用方静默吞掉）。"""
    req = Request(_PYPI_URL, headers={"User-Agent": f"mat-uitest/{__version__}"})
    with urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 —— URL 是写死的常量，无注入面
        data = json.loads(resp.read().decode("utf-8"))
    return str((data.get("info") or {}).get("version") or "")


def start_background_check() -> None:
    """serve() 启动时调用：后台守护线程查一次，成功与否都不阻塞/不影响服务。"""
    if os.environ.get("MAT_NO_UPDATE_CHECK", "").strip() == "1":
        return

    def _run() -> None:
        global _result
        try:
            latest = _check_once()
        except Exception:
            return  # 断网/超时 → 保持 None，前端不提示（零打扰）
        with _lock:
            _result = {"latest": latest,
                       "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}

    threading.Thread(target=_run, name="mat-update-check", daemon=True).start()


def api_version_check() -> dict:
    """GET /api/version/check：左上角版本号与新版 badge 的数据源。"""
    with _lock:
        snapshot = dict(_result) if _result else None
    latest = (snapshot or {}).get("latest") or ""
    enabled = os.environ.get("MAT_NO_UPDATE_CHECK", "").strip() != "1"
    # 版本比较用段元数（0.6.10 > 0.6.9）；任一端解析失败退化为不等比较
    lt, cur = _ver_tuple(latest), _ver_tuple(__version__)
    has_update = (lt > cur) if (lt and cur) else bool(latest and latest != __version__)
    return {
        "current": __version__,
        "latest": latest or None,
        "has_update": bool(enabled and has_update),
        "upgrade_cmd": _upgrade_command(),
        "checked_at": (snapshot or {}).get("checked_at"),
        "enabled": enabled,
    }
