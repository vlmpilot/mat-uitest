"""stdio 编码兜底（2026-10-25）：Windows 上「打不出 ✅」不能变成 traceback。

为什么必须有一条**子进程**测试：这是平台行为，单元替身里 reconfigure 永远成功，会把
真实崩溃掩盖掉。实证（CI run 40166970898 step5，2026-10-25）：Windows runner 的 stdout
是 cp1252，`mat summary --self-test` 打印 '✅' 时抛 UnicodeEncodeError、退出码 2 ——
而同一份代码在 macOS 上全绿，PyPI 的 `Operating System :: Microsoft :: Windows`
当时是一条兑现不了的声称。

判据（修好后必须成立）：`PYTHONIOENCODING=cp1252` 下 `summary --self-test` 退出 0
且不出现 UnicodeEncodeError / Traceback。cp1252 是 Python 自带编解码器，Linux/macOS
上同样可复现，所以这条测试在三个平台的 CI 里都是有效判据，不是 Windows 专属。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

from insta_engine.__main__ import _harden_stdio


class _Stream:
    """最小 TextIOWrapper 替身：只记录 reconfigure 收到的参数。"""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self.kwargs: dict = {}

    def reconfigure(self, **kw) -> None:
        self.kwargs.update(kw)


def test_harden_stdio_relaxes_errors(monkeypatch):
    out, err = _Stream("cp1252"), _Stream("cp1252")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _harden_stdio()

    # 非 Windows 不动编码（尊重用户的控制台代码页），只把错误处理放宽成 replace：
    # 打不出的字符显示成 '?'，而不是让整条命令以 traceback 收场。
    assert out.kwargs == {"errors": "replace"}
    assert err.kwargs == {"errors": "replace"}


def test_harden_stdio_keeps_utf8_on_windows(monkeypatch):
    """Windows 上要连编码一起换成 UTF-8（配合 SetConsoleOutputCP(65001)）。"""
    out = _Stream("cp936")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", None)  # pythonw：stderr 可能是 None

    _harden_stdio()

    assert out.kwargs == {"encoding": "utf-8", "errors": "replace"}


def test_harden_stdio_survives_exotic_streams(monkeypatch):
    """StringIO / 已 detach 的流没有 reconfigure —— 不能因此让 CLI 起不来。"""

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    _harden_stdio()  # 不抛即通过


def test_self_test_does_not_crash_on_legacy_codepage(tmp_path):
    """真实判据：cp1252 下 `mat summary --self-test` 必须退出 0。"""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        # 沙箱垫片：PYTHONHOME/PYTHONPATH 泄漏进子解释器会让它 import 不到包
        "PYTHONHOME": "",
        "PYTHONPATH": "",
    }
    env = {k: v for k, v in env.items() if v}
    proc = subprocess.run(
        [sys.executable, "-m", "insta_engine", "summary", "--self-test"],
        cwd=tmp_path, env=env, capture_output=True, timeout=180, check=False,
    )
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert proc.returncode == 0, f"exit={proc.returncode}\n{text[-800:]}"
    assert "UnicodeEncodeError" not in text
    assert "Traceback" not in text
