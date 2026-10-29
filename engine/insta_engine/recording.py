"""执行录屏（发起人拍板：全程录 + 只留失败用例）。

平台分派（make_recorder 探测可用性，返回 (recorder, reason)）：
- Android 主路径 **scrcpy --record**（无显示录制，任意真机可用，需本机装 scrcpy）；
  回退 `adb shell screenrecord`（部分国产 ROM 已裁剪该二进制——华为 EMUI 实测不存在）；
  两条都没有 ⇒ (None, 原因)，调用方诚实降级为无录屏（事件流可见）。
- iOS 模拟器：`xcrun simctl io booted recordVideo --codec h264`，SIGINT 优雅收尾
  （文件直落本机）。**iOS 真机不支持且不打算做**（2026-10-18 定论）：没有公开录制 API，
  截图 + 系统日志已是足够证据；拿低帧率截图拼视频只会让人误以为有录屏。

Android stop 语义（实测教训）：**kill 本地 adb 客户端不会停设备端 screenrecord**——
必须 `pidof` 找到设备侧进程发 SIGINT 才会封装出完整 mp4；本地客户端随之退出。

生命周期（Runner 调用）：start() → 运行用例 → stop() →
- 失败/中止：`keep(artifacts_dir)` 段文件挪进 `artifacts/video/`（报告相对引用）；
- 通过：`discard()` 全部段删除（磁盘纪律——通过的录屏价值低，看截图即可）。

子进程纪律：Popen 列表参数不经 shell；stop 的 terminate→wait 有超时兜底，
kill 后台残留由 OS 回收；段文件先落设备端暂存名（带 uuid 防串扰）。
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

MAX_SEGMENTS = 4
SEGMENT_SECONDS = 180


class Recorder:
    """按平台分派的执行录屏器。staging=段文件暂存目录（本次运行的 artifacts/rec/）。"""

    def __init__(self, platform: str, serial: str | None, staging: Path,
                 mode: str = "screenrecord", rotate_watch: bool = True):
        self.platform = platform
        self.serial = serial
        self.staging = Path(staging)
        self.rotate_watch = rotate_watch  # 测试关掉段轮转守护（确定性）
        self.mode = mode  # android: "scrcpy" | "screenrecord"
        self._proc: subprocess.Popen | None = None
        self._device_paths: list[str] = []     # Android：设备端段路径（轮转会追加）
        self._seg = 0
        self._stopping = False
        self._started = False

    # -- 生命周期 --------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self.staging.mkdir(parents=True, exist_ok=True)
        self._started = True
        self._start_segment()
        if self.platform == "android" and self.rotate_watch:
            # 段轮转守护：180s 上限到点子进程自退 ⇒ 未在停止中就换下一段。
            import threading

            threading.Thread(target=self._rotate_watch, daemon=True).start()

    def stop(self) -> list[Path]:
        """停止并收齐段文件到暂存目录；返回段文件列表（给 keep/discard）。"""
        if not self._started:
            return []
        self._stopping = True
        if self.platform == "ios":
            self._stop_ios_sim()
        elif self.mode == "scrcpy":
            proc = self._proc
            if proc is not None:
                try:
                    proc.terminate()  # scrcpy 对 SIGTERM 优雅收尾（封装 mp4）
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    pass
        else:
            self._stop_android()
        return sorted(self.staging.glob("*.mp4"))

    def keep(self, files: list[Path], artifacts_dir: Path) -> list[Path]:
        """失败/中止：段文件挪进 `artifacts_dir/video/`（报告相对引用）。"""
        dest = Path(artifacts_dir) / "video"
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for f in files:
            target = dest / f.name
            f.replace(target)
            out.append(target)
        self._cleanup_staging()
        return out

    def discard(self, files: list[Path]) -> None:
        """通过：段文件全部删除（磁盘纪律）。"""
        for f in files:
            Path(f).unlink(missing_ok=True)
        self._cleanup_staging()

    # -- Android ----------------------------------------------------------------

    def _adb(self, *args: str) -> list[str]:
        return ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)

    def _start_segment(self) -> None:
        if self._stopping:
            # P1-3（2026-10-24）：stop() 已置停止位 ⇒ 不再起新段。此前轮转线程
            # sleep(0.5) 后不复查就 _start_segment()，与 stop() 并发时会起一个
            # 不被 kill、不被 pull 的段（设备端进程/文件泄漏至多 180s）。入口同查兜底。
            return
        self._seg += 1
        if self.platform == "android":
            if self.mode == "scrcpy":
                # scrcpy 无显示录制：文件直落本机；stderr 留管道以便失败时取原因
                out = self.staging / f"rec_{self._seg:02d}.mp4"
                self._scrcpy_out = out
                self._proc = subprocess.Popen(
                    ["scrcpy"] + (["-s", self.serial] if self.serial else []) +
                    ["--no-display", "--no-audio", "--record", str(out),
                     "--record-format", "mp4"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True)
                time.sleep(2.0)  # scrcpy 启动慢；给失败暴露留窗口
                if self._proc.poll() is not None:
                    err = (self._proc.stderr.read() or "").strip()[-300:]
                    raise RuntimeError(f"scrcpy 录制启动失败: {err or '进程立即退出'}")
                return
            device_path = f"/sdcard/mat_rec_{uuid.uuid4().hex[:6]}_{self._seg}.mp4"
            self._device_paths.append(device_path)
            self._proc = subprocess.Popen(
                self._adb("shell", "screenrecord", "--time-limit", str(SEGMENT_SECONDS),
                          device_path),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:  # iOS 模拟器
            out = self.staging / f"rec_{self._seg:02d}.mp4"
            self._proc = subprocess.Popen(
                ["xcrun", "simctl", "io", "booted", "recordVideo",
                 "--codec", "h264", "--force", str(out)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _rotate_watch(self) -> None:
        """Android 段轮转：子进程到点自退且未在停止中 ⇒ 换下一段（封顶 MAX_SEGMENTS）。"""
        while True:
            proc = self._proc
            if proc is None:
                return
            proc.wait()
            if self._stopping or self._seg >= MAX_SEGMENTS:
                return
            time.sleep(0.5)  # 给设备端一点收尾时间
            if self._stopping:
                # P1-3（2026-10-24）：sleep 窗口内 stop() 到来 ⇒ 不再起新段——
                # 此前这里不复查，新段不被 kill/pull（设备端泄漏至多 180s）。
                return
            self._start_segment()

    def _stop_android(self) -> None:
        # ⚠️ **kill 本地 adb 客户端不会停设备端的 screenrecord**（2026-10-11 实测）：
        # terminate 本地进程后设备端仍在录制，1s 后 pull 一个"还在写"的文件 ⇒ 拉不到。
        # 正确做法：pidof 找到**设备侧** screenrecord → `kill -INT`（SIGINT 才会优雅封装
        # 出完整 mp4）；本地客户端会随设备端一起退出。
        proc = self._proc
        pids: list[str] = []
        try:
            out = subprocess.run(self._adb("shell", "pidof", "screenrecord"),
                                 capture_output=True, text=True, timeout=10)
            pids = (out.stdout or "").split()
        except Exception:
            pass
        if pids:
            try:
                subprocess.run(self._adb("shell", "kill", "-INT", *pids),
                               capture_output=True, timeout=10)
            except Exception:
                pass
        if proc is not None:
            try:
                proc.wait(timeout=15)  # 设备端结束 ⇒ 本地客户端随之退出
            except subprocess.TimeoutExpired:
                proc.kill()
        time.sleep(1.0)  # 设备端封装落盘
        for seg, device_path in enumerate(self._device_paths, 1):
            try:
                subprocess.run(self._adb("pull", device_path,
                                         str(self.staging / f"rec_{seg:02d}.mp4")),
                               capture_output=True, timeout=60, check=True)
            except Exception:
                pass  # 单段拉取失败就放弃该段（其余段照常）
            try:
                subprocess.run(self._adb("shell", "rm", "-f", device_path),
                               capture_output=True, timeout=15)
            except Exception:
                pass

    # -- iOS 模拟器 ---------------------------------------------------------------

    def _stop_ios_sim(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.send_signal(__import__("signal").SIGINT)  # 优雅收尾（文件封装完整）
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    def _cleanup_staging(self) -> None:
        try:
            for f in self.staging.glob("*.mp4"):
                f.unlink(missing_ok=True)
            self.staging.rmdir()
        except OSError:
            pass


def make_recorder(platform: str, serial: str | None,
                  artifacts_dir: Path) -> tuple[Recorder | None, str | None]:
    """按平台探测可用性并构造录屏器；返回 (recorder | None, 不可用原因 | None)。

    - android：scrcpy 已装 ⇒ scrcpy 模式（任意真机可用）；否则设备有 screenrecord
      二进制 ⇒ screenrecord 模式；两者皆无 ⇒ None + 精确原因（华为 EMUI 实测裁剪了
      screenrecord，2026-10-11）。
    - ios：模拟器（WDA 驱动的 serial="simulator"）⇒ simctl 录制；真机 ⇒ None（P2）。
    """
    staging = Path(artifacts_dir) / "rec"
    if platform == "android":
        if shutil.which("scrcpy"):
            return Recorder("android", serial, staging, mode="scrcpy"), None
        try:
            probe = subprocess.run(
                ["adb"] + (["-s", serial] if serial else []) +
                ["shell", "command", "-v", "screenrecord"],
                capture_output=True, text=True, timeout=10)
            if (probe.stdout or "").strip():
                return Recorder("android", serial, staging, mode="screenrecord"), None
        except Exception:
            pass
        return None, ("该设备不支持录屏：设备端无 screenrecord，且本机未安装 scrcpy"
                      "（可选装后重试；模拟器/其他设备通常可用）")
    if platform == "ios":
        if serial and serial != "simulator":
            # 2026-10-18 定论：**没有公开 API**——`simctl io recordVideo` 只对模拟器有效，
            # `devicectl` 无录屏子命令，`xctrace` 录的是 Instruments 轨迹不是视频。
            # 不拿"2fps 截图拼视频"冒充录屏：那种产物对定位问题没有价值，还会让人误以为有录屏。
            return None, ("iOS 真机不支持录屏：系统没有公开的录制 API"
                          "（simctl 只对模拟器有效，xctrace 录的是 Instruments 轨迹）。"
                          "失败步骤的**截图 + 系统日志**仍然完整，可作为证据")
        return Recorder("ios", serial, staging), None
    if platform == "harmony":
        # 2026-10-26 鸿蒙首版：uitest 无公开录制子命令——诚实降级(同 iOS 真机先例)，
        # 失败步骤的**截图**仍是完整证据;后续有公开 API 再补
        return None, "鸿蒙首版不支持录屏（失败步骤的截图证据仍完整）"
    return None, f"未知平台 {platform!r}"
