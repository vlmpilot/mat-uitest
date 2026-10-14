"""报告分享域：/api/share（局域网分享状态与链接，T55 P0 的配置台入口）。"""

from __future__ import annotations

from .runtime import lan_urls


def api_share(host: str, port: int) -> tuple[int, dict]:
    """GET /api/share：当前监听形态与局域网可达链接。

    `mat console --share`（host=0.0.0.0）时 urls 可被同一 Wi-Fi 的同事直接打开
    （零出域；无鉴权，启动时已有警示）；默认 127.0.0.1 时 urls 仅本机可达——
    前端据此展示「如何开启分享」的指引，而不是假装链接可点。
    """
    shared = host not in ("127.0.0.1", "localhost")
    return 200, {
        "shared": shared,
        "host": host,
        "port": port,
        "urls": lan_urls(port) if shared else [],
        "hint": ("局域网内任何浏览器打开以下链接即可查看报告（数据不出内网，无鉴权——"
                 "请只在可信网络使用）" if shared else
                 "以 `mat console --share` 启动可把报告分享给同一 Wi-Fi 的同事（数据不出内网）。"),
    }
