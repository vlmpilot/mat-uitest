"""配置台「页面复核」的 HTTP 层测试：真服务器 + 临时工作区，不碰真机、不碰真图谱。

为什么单独一个文件：`test_web_api.py` 覆盖的是既有端点，这里是**新端点**
（`/api/review` 的 GET/POST）。分开写也避免与并行会话改同一个文件。

⚠️ 这些用例都在锁「错了会静默」的点：
- **路径穿越**（`../`、`a/b`）必须被 400 挡住 —— 否则能读/写工作区外的图谱；
- **图谱不存在**要 404 且给可行动的提示，不能 500；
- **`corrections` 不是对象**要 400（前端传错形状时不能静默什么也不做）；
- **幂等**：把标题改成它现在的值 ⇒ `applied=0`、`unchanged=1`；
- **找不到的 `page_key` 要回给前端**（图谱重跑后标识会变，静默丢弃 = 用户白复核）；
- **截图 URL 真的能取到**（前端要显示它，给个 404 的 URL 等于没有截图）。
"""

from __future__ import annotations

import json
import threading
import time

import pytest
import requests

from insta_engine import web_server


def _graph(pages: list[dict]) -> dict:
    return {"schema": 2, "package": "com.demo", "pages": pages}


def _page(key: str, title: str, shot: str = "") -> dict:
    return {"page_key": key, "title": title, "depth": 0,
            "activity": "com.demo.ProductListActivity", "screenshot": shot,
            "summary": "示例描述"}


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """真 HTTP 服务器（内核分配端口 + 临时工作区）。跑完必须关掉。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    idx = tmp_path / "artifacts" / "page_index"
    shots = idx / "screenshots" / "com.demo"
    shots.mkdir(parents=True)
    (shots / "a.png").write_bytes(_tiny_png())
    (idx / "com.demo.json").write_text(json.dumps(_graph([
        _page("k1", "被子搜索页", "screenshots/com.demo/a.png"),
        _page("k2", "枕头搜索页", "screenshots/com.demo/a.png"),
    ]), ensure_ascii=False), encoding="utf-8")

    server = web_server.make_server(0, root=tmp_path)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            requests.get(f"{base}/api/review?package=com.demo", timeout=5)
            break
        except requests.ConnectionError:
            time.sleep(0.1)
    yield base, tmp_path
    server.shutdown()
    server.server_close()


# -- GET --------------------------------------------------------------------------

def test_get_returns_pages_with_screenshot_url(srv) -> None:
    base, _ = srv
    r = requests.get(f"{base}/api/review?package=com.demo", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert [p["page_key"] for p in d["pages"]] == ["k1", "k2"]
    assert d["pages"][0]["title"] == "被子搜索页"
    assert d["pages"][0]["screenshot_url"] == \
        "/artifacts/page_index/screenshots/com.demo/a.png", "前端要靠这个 URL 显示截图"
    assert d["manual"] == 0 and d["saved"] == {}
    assert d["review_path"].endswith("com.demo.json")


def test_get_rejects_path_traversal(srv) -> None:
    """**路径穿越必须被挡**——否则能拿工作区外的图谱。"""
    base, _ = srv
    for bad in ("", "../etc", "a/b", "..%2Fetc"):
        r = requests.get(f"{base}/api/review?package={bad}", timeout=10)
        assert r.status_code == 400, f"package={bad!r} 应被拒，实际 {r.status_code}"


def test_get_missing_graph_is_404_with_hint(srv) -> None:
    base, _ = srv
    r = requests.get(f"{base}/api/review?package=com.nope", timeout=10)
    assert r.status_code == 404
    assert "mat index" in r.json()["error"], "报错要给出可行动的下一步"


def test_screenshot_url_is_actually_served(srv) -> None:
    """给前端的截图 URL **必须真能取到** —— 404 的 URL 等于没有截图。"""
    base, _ = srv
    d = requests.get(f"{base}/api/review?package=com.demo", timeout=10).json()
    r = requests.get(f"{base}{d['pages'][0]['screenshot_url']}", timeout=10)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert len(r.content) > 0


# -- POST -------------------------------------------------------------------------

def test_post_applies_and_persists(srv) -> None:
    base, root = srv
    r = requests.post(f"{base}/api/review", timeout=10,
                      json={"package": "com.demo", "operator": "测试员",
                            "corrections": {"k1": "搜索结果页", "k2": "搜索结果页"}})
    assert r.status_code == 200 and r.json()["applied"] == 2
    # 图谱被写回（标 manual）
    idx = json.loads((root / "artifacts/page_index/com.demo.json").read_text(encoding="utf-8"))
    assert [p["title"] for p in idx["pages"]] == ["搜索结果页", "搜索结果页"]
    assert all(p["title_source"] == "manual" for p in idx["pages"])
    # 结论落盘到 review/（与图谱解耦）
    store = json.loads((root / "review/com.demo.json").read_text(encoding="utf-8"))
    assert store["corrections"] == {"k1": "搜索结果页", "k2": "搜索结果页"}


def test_post_is_idempotent(srv) -> None:
    base, _ = srv
    body = {"package": "com.demo", "corrections": {"k1": "搜索结果页"}, "operator": "测试员"}
    assert requests.post(f"{base}/api/review", json=body, timeout=10).json()["applied"] == 1
    again = requests.post(f"{base}/api/review", json=body, timeout=10).json()
    assert again["applied"] == 0 and again["unchanged"] == 1, "重复保存不许再改一遍"


def test_post_reports_missing_keys(srv) -> None:
    """图谱重跑后 `page_key` 会变 ⇒ **必须逐条回给前端**，不能静默丢弃。"""
    base, _ = srv
    r = requests.post(f"{base}/api/review", timeout=10,
                      json={"package": "com.demo", "operator": "测试员",
                            "corrections": {"k1": "A", "幽灵": "B"}})
    assert r.json()["missing"] == ["幽灵"]


def test_post_rejects_bad_input(srv) -> None:
    base, _ = srv
    cases = [({"package": "", "corrections": {}}, 400),
             ({"package": "a/b", "corrections": {}}, 400),
             ({"package": "com.demo"}, 400),                 # 缺 corrections
             ({"package": "com.demo", "corrections": "x"}, 400),   # 形状不对
             ({"package": "com.nope", "corrections": {}}, 404)]
    for body, want in cases:
        r = requests.post(f"{base}/api/review", json={**body, "operator": "测试员"}, timeout=10)
        assert r.status_code == want, f"{body} 应为 {want}，实际 {r.status_code}"


def test_post_accumulates_across_sessions(srv) -> None:
    """两次复核不许互相覆盖 —— 第二次要保留第一次的结论。"""
    base, root = srv
    requests.post(f"{base}/api/review", timeout=10,
                  json={"package": "com.demo", "operator": "测试员",
                        "corrections": {"k1": "A"}})
    requests.post(f"{base}/api/review", timeout=10,
                  json={"package": "com.demo", "operator": "测试员",
                        "corrections": {"k2": "B"}})
    store = json.loads((root / "review/com.demo.json").read_text(encoding="utf-8"))
    assert store["corrections"] == {"k1": "A", "k2": "B"}, "累积落盘，第二次不许把第一次冲掉"


def _tiny_png(w: int = 20, h: int = 32) -> bytes:
    import struct
    import zlib
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw += bytes(((x * 8) % 256, (y * 5) % 256, 120))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
