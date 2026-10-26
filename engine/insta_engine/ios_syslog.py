"""iOS syslog 证据采集（P2.2）：后台读设备日志，环形缓冲 + 失败重启。

**为什么需要它**：iOS 没有 logcat，而 `app-crash` 的硬证据（崩溃/未捕获异常）只在系统
日志里。WDA 不提供日志流；唯一稳定的黑盒来源是 `pymobiledevice3 syslog`——它走 usbmux
的 `syslog_relay`，纯 Python，不依赖 brew/libimobiledevice（与本机约束一致）。

**边界（诚实，已写进使用手册）：
- 未装 `pymobiledevice3` ⇒ `start()` 返回 False、`tail()` 返回 `""`（**"没有证据源" ≠
  "没有日志"**，上层必须把空如实暴露成 warning）；
- iOS 17+ 的 syslog_relay 是否需要隧道**未实测**（设备当前不可用）；失败会有限次重启后放弃，
  **绝不阻塞执行**；
- 只保留尾部 N 行（环形缓冲），长跑不吃内存。
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
import time
from collections import deque

#: 环形缓冲默认行数（一次失败通常只需要最后几百行；4000 行约 0.5MB）
DEFAULT_MAX_LINES = 4000

#: 失败步取证据的行数。
#: ⚠️ 不能用 200：iOS syslog 实测约 **500 行/秒**（8 秒填满 4000 行环形缓冲），
#: 200 行只覆盖 ~0.4 秒——崩溃发生在失败前 5 秒就完全看不到。
#: 2000 行 ≈ 4 秒窗口，且仍远小于缓冲上限。
EVIDENCE_TAIL_LINES = 2000

#: 进程意外退出后的重启次数上限（设备拔插/服务重启是常态，但不能无限重启）
MAX_RESTARTS = 3

#: 每个 udid 只保留一个采集器（console 长跑会反复建驱动，避免进程泄漏）
_BUFFERS: dict[str, SyslogBuffer] = {}
_LOCK = threading.Lock()


class SyslogBuffer:
    """后台 syslog 采集器：线程读 stdout → 环形缓冲。"""

    def __init__(self, udid: str, max_lines: int = DEFAULT_MAX_LINES,
                 cmd: list[str] | None = None, max_restarts: int = MAX_RESTARTS):
        self.udid = udid
        self._buf: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        # 两个坑（2026-10-18 实测）：
        # ① 用 `python -m` 而不是裸命令——pip 装在 venv 里时**不在 PATH**
        #    （`shutil.which` 返回 None ⇒ 采集静默不启动）；
        # ② v11 的 `syslog live` **不接受 `--udid`**，设备靠环境变量
        #    `PYMOBILEDEVICE3_UDID` 选（实测传 --udid 会 "No such option"）。
        self._cmd = cmd or [sys.executable, "-m", "pymobiledevice3", "syslog", "live"]
        self._env = dict(os.environ, PYMOBILEDEVICE3_UDID=udid)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._restarts = 0
        self._max_restarts = max_restarts

    # -- 可用性 ---------------------------------------------------------------

    @staticmethod
    def available() -> bool:
        """能 import 就算可用——**不要用 `shutil.which`**（venv 里的脚本不在 PATH）。"""
        try:
            import pymobiledevice3  # noqa: F401
            return True
        except ImportError:
            return False

    # -- 生命周期 -------------------------------------------------------------

    def start(self) -> bool:
        """起采集线程。返回是否成功启动（失败**不抛**，调用方据此写 warning）。"""
        if not self.available():
            return False
        return self._spawn()

    def _spawn(self) -> bool:
        try:
            self._proc = subprocess.Popen(
                self._cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                env=getattr(self, "_env", None))
        except (OSError, subprocess.SubprocessError):
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True

    def _pump(self) -> None:
        """读 stdout 直到进程结束；异常退出时有限次重启（拔插/服务重启是常态）。"""
        while not self._stopped:
            proc = self._proc
            if proc is None or proc.stdout is None:
                break
            for line in proc.stdout:
                with self._lock:
                    self._buf.append(line.rstrip("\n"))
                if self._stopped:
                    break
            if self._stopped:
                break
            self._restarts += 1
            if self._restarts > self._max_restarts:
                break
            time.sleep(1)
            if not self._spawn():
                break

    def stop(self) -> None:
        self._stopped = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    # -- 数据面 ---------------------------------------------------------------

    def clear(self) -> None:
        """清空缓冲（对齐 logcat -c 的"时效限定"语义：只留本轮）。"""
        with self._lock:
            self._buf.clear()

    def tail(self, lines: int = 200) -> str:
        with self._lock:
            data = list(self._buf)
        return "\n".join(data[-lines:])

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


def get_buffer(udid: str) -> SyslogBuffer:
    """取（或新建）该设备的采集器；**同 udid 旧实例会被停掉**，避免进程堆积。"""
    with _LOCK:
        old = _BUFFERS.get(udid)
        if old is not None:
            old.stop()
        buf = SyslogBuffer(udid)
        _BUFFERS[udid] = buf
        atexit.register(buf.stop)
        return buf


def reset() -> None:
    """停掉全部采集器（测试与长跑收尾用）。"""
    with _LOCK:
        for buf in _BUFFERS.values():
            buf.stop()
        _BUFFERS.clear()
