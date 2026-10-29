"""hdc(HarmonyOS Device Connector)命令行封装——鸿蒙链路的 adb.py 对应物。

纪律与 adb.py 一致:纯 subprocess、显式超时、编码容错(GBK/UTF-8)、不吞错误。
命令规范以华为公开文档为准;**字段名与个别命令行为以真机 spike 校准为准**
(校准点集中在 `_run` 的调用方)。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: 鸿蒙设备上的暂存路径(dumpLayout / snapshot 的中转)
DEVICE_TMP = "/data/local/tmp/"

#: DevEco Studio 的标准安装位置(macOS)——hdc 随 SDK 分发,常不在用户 PATH
_DEVECO_HDC_CANDIDATES = [
    "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc",
]

#: 环境变量兜底:非标准安装时用户可显式指定 hdc 可执行文件路径
ENV_HDC = "MAT_HDC"


def hdc_path() -> str | None:
    """解析 hdc 可执行文件:MAT_HDC > PATH > DevEco 标准安装位置。"""
    env = os.environ.get(ENV_HDC)
    if env and Path(env).exists():
        return env
    found = shutil.which("hdc")
    if found:
        return found
    for cand in _DEVECO_HDC_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def hdc_available() -> bool:
    """hdc 是否可用(不存在 ⇒ 鸿蒙链路整体不可用,调用方据此显式报错)。"""
    return hdc_path() is not None


def _run(args: list[str], serial: str | None = None, timeout: int = 20) -> subprocess.CompletedProcess:
    """执行一条 hdc 命令。多设备时必须带 ``-t <key>``(与 adb -s 同语义)。

    hdc 的返回码纪律与 adb 不同:**shell 内命令失败也常回 0**,错误只写在
    stderr/stdout 文本里 ⇒ 调用方对关键结果要判内容,不能只判 returncode。
    """
    binpath = hdc_path()
    if not binpath:
        raise FileNotFoundError("hdc 不在 PATH 且未找到 DevEco 安装(MAT_HDC 可显式指定)")
    cmd = [binpath]
    if serial:
        cmd += ["-t", serial]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def list_targets() -> list[str]:
    """连接中的设备 target key 列表(`hdc list targets`;Empty ⇒ 无设备)。"""
    proc = _run(["list", "targets"], timeout=10)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines()
            if line.strip() and line.strip() != "[Empty]"]


def shell(command: str, serial: str | None = None, timeout: int = 20) -> str:
    """`hdc shell <command>` 的 stdout;取不到时抛 RuntimeError(调用方决定降级)。"""
    proc = _run(["shell", command], serial=serial, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"hdc shell 失败(rc={proc.returncode}): {(proc.stderr or proc.stdout or '').strip()[:200]}")
    return proc.stdout


def recv(remote: str, local: str, serial: str | None = None, timeout: int = 30) -> None:
    """从设备拉文件到本地(`hdc file recv`);失败抛 RuntimeError。"""
    proc = _run(["file", "recv", remote, local], serial=serial, timeout=timeout)
    if proc.returncode != 0 or not Path(local).exists():
        raise RuntimeError(f"hdc file recv 失败: {(proc.stderr or proc.stdout or '').strip()[:200]}")
