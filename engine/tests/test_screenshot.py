"""`mat_screenshot`：截图返回**图**而不只是路径（协议 v3 §3.2）。

三条纪律各有一条测试兜着：不缩边会几十万 token（默认必须缩）、只读不抛异常、
产物目录不清理会堆成垃圾场。这里全部用假驱动，不依赖真机。
"""

from __future__ import annotations

import json
import os
import time

from PIL import Image

from insta_engine import driver_factory
from insta_engine import verify as v


class _ShotDriver:
    """只实现截图与前台包名：其他接口不该被这个功能碰到。"""

    serial = "serial-a"
    platform = "android"

    def __init__(self, size=(2000, 1000), fail: bool = False):
        self.size = size
        self.fail = fail
        self.calls = 0

    def screenshot(self, path: str) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("adb 断连")
        Image.new("RGB", self.size, (30, 120, 200)).save(path, format="PNG")

    def foreground_app(self) -> dict:
        return {"package": "com.demo.app"}


def test_no_device_returns_error_not_raise(monkeypatch, tmp_path):
    """无设备时如实返回 ok=false：agent 要能读到原因，而不是吃一个异常。"""

    def boom(*_a, **_kw):
        raise RuntimeError("no devices")

    monkeypatch.setattr(driver_factory, "connect_driver", boom)
    res = v.capture_screen(tmp_path)
    assert res["ok"] is False
    assert "设备连接失败" in res["error"]


def test_capture_writes_original_and_shrinks_thumbnail(monkeypatch, tmp_path):
    """原图落盘 + 内联缩略图：默认缩到 1280，且用的是 JPEG（PNG 内联太贵）。"""
    drv = _ShotDriver(size=(2000, 1000))
    monkeypatch.setattr(driver_factory, "connect_driver", lambda s, p: drv)

    res = v.capture_screen(tmp_path)
    assert res["ok"] is True
    assert (res["width"], res["height"]) == (2000, 1000)
    assert max(res["thumb_width"], res["thumb_height"]) == v.SCREENSHOT_MAX_EDGE
    assert res["foreground"] == "com.demo.app"
    assert res["path"].startswith("artifacts/verify/_screens/")
    assert (tmp_path / res["path"]).exists(), "原图必须留下，供人事后核对"

    assert res["jpeg"][:2] == b"\xff\xd8", "内联图必须是 JPEG"
    import io

    thumb = Image.open(io.BytesIO(res["jpeg"]))
    assert thumb.size == (res["thumb_width"], res["thumb_height"])


def test_screenshot_failure_returns_error(monkeypatch, tmp_path):
    """截图本身失败（断连/权限）也只返回 ok=false——不能把异常抛给 MCP 层。"""
    monkeypatch.setattr(driver_factory, "connect_driver", lambda s, p: _ShotDriver(fail=True))
    res = v.capture_screen(tmp_path)
    assert res["ok"] is False
    assert "截图失败" in res["error"]


def test_prune_keeps_newest_and_drops_expired(tmp_path):
    """清理：超量删最旧、超期直接删；删不动也不影响返回（这里只验数量）。"""
    shots = tmp_path / "shots"
    shots.mkdir()
    for i in range(5):
        p = shots / f"{i}.png"
        p.write_bytes(b"x")
        os.utime(p, (time.time() - i * 10, time.time() - i * 10))

    removed = v._prune_screens(shots, keep=2, ttl_seconds=10 ** 6)
    assert removed == 3
    assert sorted(p.name for p in shots.glob("*.png")) == ["0.png", "1.png"]

    # 超期：把剩下的改成 8 天前，TTL 7 天 ⇒ 全删
    old = time.time() - 8 * 24 * 3600
    for p in shots.glob("*.png"):
        os.utime(p, (old, old))
    assert v._prune_screens(shots, keep=200, ttl_seconds=7 * 24 * 3600) == 2


def test_content_list_shape_is_fixed(monkeypatch, tmp_path):
    """返回形态固定为 list：1 个文本元数据 +（可选）1 张图；文本里不塞二进制。"""
    monkeypatch.setattr(driver_factory, "connect_driver", lambda s, p: _ShotDriver(size=(800, 600)))
    res = v.capture_screen(tmp_path)

    content = v.build_screenshot_content(res, include_image=True)
    assert [c.type for c in content] == ["text", "image"]
    meta = json.loads(content[0].text)
    assert "jpeg" not in meta and meta["ok"] is True
    assert content[1].mime_type == "image/jpeg"   # 字段名是 mime_type，序列化时按 mimeType 输出

    only_meta = v.build_screenshot_content(res, include_image=False)
    assert [c.type for c in only_meta] == ["text"], "关掉图时不返回空 image 块"

    # 失败的返回同样只给文本，不给半个空图
    failed = v.build_screenshot_content({"ok": False, "error": "x"})
    assert [c.type for c in failed] == ["text"]


def test_mcp_tool_returns_text_and_image(monkeypatch, tmp_path):
    """在**真实 MCP 服务**上验证契约：工具已注册、返回 text+image、关图只剩 text。

    只测 `build_screenshot_content` 不够——MCP SDK 的返回值转换是另一层，
    它可能把 list 当 JSON 序列化成文本（那样 agent 依然看不到图）。
    """
    import asyncio
    import io

    from insta_engine.mcp_server import build_server

    monkeypatch.chdir(tmp_path)          # 避免读到仓库根的 .env，污染其他用例

    def fake_capture(root, *, serial=None, platform="android", max_edge=1280):
        buf = io.BytesIO()
        Image.new("RGB", (640, 320), (1, 2, 3)).save(buf, format="JPEG")
        return {"ok": True, "path": "artifacts/verify/_screens/x.png", "width": 640,
                "height": 320, "thumb_width": 640, "thumb_height": 320,
                "foreground": "com.demo", "taken_at": "2026-10-30T12:00:00+00:00",
                "max_edge": max_edge, "jpeg": buf.getvalue()}

    monkeypatch.setattr(v, "capture_screen", fake_capture)
    server = build_server(tmp_path)

    async def run():
        tools = [t.name for t in await server.list_tools()]
        assert "mat_screenshot" in tools
        with_image = await server.call_tool("mat_screenshot", {"include_image": True})
        meta_only = await server.call_tool("mat_screenshot", {"include_image": False})
        return with_image, meta_only

    with_image, meta_only = asyncio.run(run())
    assert [c.type for c in with_image.content] == ["text", "image"]
    assert with_image.content[1].mime_type == "image/jpeg"
    assert [c.type for c in meta_only.content] == ["text"]
