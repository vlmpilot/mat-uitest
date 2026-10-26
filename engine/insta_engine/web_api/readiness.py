"""/api/readiness GET（图谱准备度域）——只读状态 + 建议，不设闸门参见 readiness 模块说明。"""

from __future__ import annotations

from pathlib import Path


def api_readiness(root: Path, package: str) -> tuple[int, dict]:
    """返回某包的图谱准备度 + 一行摘要 + 一条建议。**只读**，不需要设备与模型。"""
    from .. import readiness as rd

    if not package or "/" in package or ".." in package:
        return 400, {"error": "非法包名"}
    r = rd.readiness(root, package)
    # ui="console"：控制台用户在界面上——建议给页面/按钮，别让他去终端敲命令
    return 200, {**r, "text": rd.summary(r), "hint": rd.hint(r, ui="console"),
                 "explore_tip": rd.EXPLORE_TIP}
