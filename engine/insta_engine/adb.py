"""adb 调用统一封装（原先散在 3 处，收敛于此以免口径漂移）。

全部 best-effort：失败返回 None/空串，不阻塞主流程（与 perf.py 的原始设计一致）。
"""

from __future__ import annotations

import shlex
import subprocess


def adb(serial: str | None, *args: str, timeout: float = 15) -> str | None:
    """执行 adb 命令并返回 stdout；失败返回 None。"""
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")  # Windows 默认 GBK 会炸中文日志
        return proc.stdout if proc.returncode == 0 else None
    except FileNotFoundError:
        # 本机没有 CLI `adb`（2026-10-25 真机评测）：回退 adbutils——驱动能连设备，
        # 说明只是**采集通道**缺失；不兜底会让失败步日志静默变空、崩溃归因失效。
        return _adb_py(serial, args, timeout)
    except Exception:
        return None


def _shell_command(args: tuple[str, ...]) -> str:
    """把 adb 参数列表转成**设备端** shell 命令行（CLI 缺失时的回退通道用）。

    ⚠️ **必须先剥掉开头的 `shell`**（2026-10-25 真机实测）：adbutils 的
    `device.shell(cmd)` 本身就在设备 shell 里执行，再传一个 `shell` 就变成
    `sh -c "shell dumpsys meminfo …"` ⇒ 报
    `/system/bin/sh: shell: inaccessible or not found`。而 `adb()` 只在
    returncode==0 时返回 stdout ⇒ **静默变空**。
    受影响的是带 `shell` 前缀的调用方（perf 的启动耗时 / meminfo、
    recording 的 screenrecord 控制等）——在没装 adb CLI 的机器上（本机就是）
    这些采集会全部静默丢数据，而**驱动本身照常能连设备**，所以看起来一切正常。
    """
    rest = args[1:] if args and args[0] == "shell" else args
    return " ".join(shlex.quote(a) for a in rest)


def _adb_py(serial: str | None, args: tuple[str, ...], timeout: float) -> str | None:
    """CLI `adb` 不可用时的纯 Python 通道（adbutils，与驱动同一传输）。"""
    try:
        import adbutils

        device = adbutils.adb.device(serial) if serial else adbutils.adb.device()
        return device.shell(_shell_command(args), timeout=timeout)
    except Exception:
        return None


#: logcat 的默认优先级过滤：**只取 Error 以上**（含 F/S）。
#:
#: ⚠️ **这是必须的，不是优化**。实测（2026-10-07，崩溃类探针 T76）：
#: 不过滤时，末 300 行**被噪声占满**——`AccessibilityNodeInfoDumper: Skipping invisible child`
#: （**dump 自己产生的**，每页几百行）、`TrafficMonitor`、音频日志…
#: ⇒ 崩溃被挤出窗口，**`app-crash` 分类在真机上失效**：
#: 那次崩溃在缓冲区第 **3083** 行、末尾共 **7177** 行（距末尾 ~4100 行），
#: 而窗口只有 300 行 ⇒ 探针**检出了**崩溃，却被归成 `assert-fail`。
LOGCAT_PRIORITY = "E"

#: logcat 的默认行数窗口。⚠️ **`-t` 是"先取末 N 行原始日志、再按优先级过滤"**
#: （实测：`-t 200 *:E` 只剩 **15** 行、拿不到崩溃；`-t 5000 *:E` 有 **663** 行、拿到了）。
#: ⇒ **过滤救不了小窗口**，两者必须一起给。取 10000 ≈ 覆盖整段缓冲区（实测 937 行 / 99KB / 0.34s）。
LOGCAT_TAIL_LINES = 10000


def logcat_tail(serial: str | None, lines: int = LOGCAT_TAIL_LINES,
                priority: str | None = LOGCAT_PRIORITY) -> str:
    """读取 logcat 末段（崩溃/异常证据来源）；失败返回空串。

    `priority` 见 `LOGCAT_PRIORITY` 的说明——**默认过滤到 Error 以上**。
    传 `None` 可取不过滤的全量末段（需要 W/I 上下文时用）。

    ⚠️ **时效问题**：窗口开到整段缓冲区后，**上一轮留下的崩溃会让这一轮的无关失败
    被误判成 `app-crash`**（正是本项目最怕的假阳性）。
    ⇒ 调用方应在**每轮开始时 `logcat_clear()`**（`Runner.run` / `explore` 都已这么做）。
    """
    args = ["logcat", "-d", "-t", str(lines)]
    if priority:
        # `*:E` 作为**单个 argv** 传入，不经 shell ⇒ 不会被 glob 展开。
        args.append(f"*:{priority}")
    return adb(serial, *args, timeout=10) or ""


def logcat_clear(serial: str | None) -> bool:
    """清空 logcat 缓冲区，**限定后续 `logcat_tail` 的时效**。

    为什么必须有（T76 踩到）：窗口开大到整段缓冲区后，"上一轮的崩溃"会被算进
    "这一轮的失败证据" ⇒ **假阳性**。清一次就干净了。

    ⚠️ 这是对**设备瞬态状态**的操作（环形缓冲区，非用户数据），且 MAT 在一轮里独占设备。
    """
    return adb(serial, "logcat", "-c", timeout=10) is not None
