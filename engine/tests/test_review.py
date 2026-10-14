"""`mat review`（人工复核）的离线测试：不碰真机、不碰真图谱。

为什么这些用例值钱：复核是**用户唯一能把"AI 起错了"说出来的入口**，
而它一旦出错**看起来完全正常**——用户的修改被静默丢弃、或被写到了错误的 App 上，
界面都不会报错。所以下面每条都在锁一个**"错了会静默"**的点：

1. **找不到的 `page_key` 必须被报出来**（图谱重跑后 key 会变，静默丢弃 = 用户白复核）；
2. **幂等**（重复应用结果一致，第二次应记为"已是目标值"而不是又改一遍）；
3. **人工标记**（`title_source="manual"`，否则 `describe` 会把用户的修改顶掉）；
4. **复核页自包含**（截图内联成 `data:` URI —— 相对路径在预览里会裂成一排裂图）；
5. **结论可往返**（导出的文件能读回来，且与图谱解耦）。
"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine import review


def _index(pages: list[dict]) -> dict:
    return {"schema": 2, "package": "com.demo", "pages": pages}


def _page(key: str, title: str) -> dict:
    return {"page_key": key, "title": title, "depth": 0,
            "activity": "com.demo.ProductListActivity", "screenshot": ""}


# -- 应用结论 ---------------------------------------------------------------------

def test_apply_sets_manual_source_and_counts_each_bucket() -> None:
    idx = _index([_page("k1", "被子搜索页"), _page("k2", "枕头搜索页"), _page("k3", "首页")])
    stats = review.apply_corrections(idx, {"k1": "搜索结果页", "k2": "枕头搜索页", "k9": "幽灵页"})
    assert stats["applied"] == ["k1"], "只有真正变了的才算 applied"
    assert stats["unchanged"] == ["k2"], "值没变要记为 unchanged（幂等的基础）"
    assert stats["missing"] == ["k9"], "**找不到的 key 必须被报出来**，不能静默丢弃"
    assert idx["pages"][0]["title"] == "搜索结果页"
    assert idx["pages"][0]["title_source"] == "manual", "必须标 manual，否则 describe 会顶掉"
    assert "title_source" not in idx["pages"][1], "没改的页不该被标 manual"


def test_apply_is_idempotent() -> None:
    idx = _index([_page("k1", "被子搜索页")])
    review.apply_corrections(idx, {"k1": "搜索结果页"})
    again = review.apply_corrections(idx, {"k1": "搜索结果页"})
    assert again["applied"] == [] and again["unchanged"] == ["k1"], "重放不许再改一遍"


def test_apply_trims_whitespace() -> None:
    idx = _index([_page("k1", "被子搜索页")])
    stats = review.apply_corrections(idx, {"k1": "  搜索结果页  "})
    assert stats["applied"] == ["k1"] and idx["pages"][0]["title"] == "搜索结果页"


def test_apply_allows_clearing_a_title() -> None:
    """留空是合法输入（"这页不需要标题"），要能清掉而不是被当成没改。"""
    idx = _index([_page("k1", "被子搜索页")])
    stats = review.apply_corrections(idx, {"k1": ""})
    assert stats["applied"] == ["k1"] and idx["pages"][0]["title"] == ""


# -- 功能描述的人工结论（2026-10-13） ---------------------------------------------------

def test_apply_summary_corrections_marks_manual() -> None:
    """描述结论与标题同规则：写入标 manual（describe/merge 均不覆盖），missing 与标题共用。"""
    idx = _index([_page("k1", "被子搜索页")])          # k9 故意不在图谱里 → missing
    stats = review.apply_corrections(idx, {"k1": "搜索结果页"},
                                     {"k1": "展示搜索结果与筛选", "k9": "幽灵描述"})
    assert stats["summary_applied"] == ["k1"] and stats["summary_unchanged"] == []
    assert stats["missing"] == ["k9"], "描述的 missing 并进同一个集合，用户看到的告警一致"
    page = idx["pages"][0]
    assert page["summary"] == "展示搜索结果与筛选"
    assert page["summary_source"] == "manual", "必须标 manual，否则 describe 会顶掉"


def test_apply_summary_is_idempotent() -> None:
    idx = _index([_page("k1", "被子搜索页")])
    review.apply_corrections(idx, {}, {"k1": "展示搜索结果"})
    again = review.apply_corrections(idx, {}, {"k1": "展示搜索结果"})
    assert again["summary_applied"] == [] and again["summary_unchanged"] == ["k1"], \
        "重放不许再改一遍"


# -- 结论文件的读写 ----------------------------------------------------------------

def test_load_corrections_accepts_both_shapes(tmp_path: Path) -> None:
    wrapped = tmp_path / "a.json"
    wrapped.write_text(json.dumps({"package": "com.demo",
                                   "corrections": {"k1": "A"}}), encoding="utf-8")
    assert review.load_corrections(wrapped)["corrections"] == {"k1": "A"}
    bare = tmp_path / "b.json"
    bare.write_text(json.dumps({"k1": "A"}), encoding="utf-8")
    got = review.load_corrections(bare)
    assert got["corrections"] == {"k1": "A"} and got["package"] == "", "裸形状也要能读"


def test_summary_corrections_roundtrip(tmp_path: Path) -> None:
    """描述结论随 store 往返：save_review 落盘 → load_corrections 读回；旧文件无键时为空。"""
    store = review.save_review("com.demo", {"k1": "A"}, tmp_path,
                               note="t", summary_corrections={"k1": "用途"})
    got = review.load_corrections(store)
    assert got["corrections"] == {"k1": "A"}
    assert got["summary_corrections"] == {"k1": "用途"}
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"corrections": {"k1": "A"}}), encoding="utf-8")
    assert review.load_corrections(legacy)["summary_corrections"] == {}, "旧文件无键 → 空 dict"


def test_save_review_round_trips(tmp_path: Path) -> None:
    path = review.save_review("com.demo", {"k1": "A"}, tmp_path, note="测试")
    assert path == tmp_path / "review" / "com.demo.json", "结论要存到 review/ 下（与图谱解耦）"
    loaded = review.load_corrections(path)
    assert loaded["corrections"] == {"k1": "A"} and loaded["package"] == "com.demo"


def test_review_path_is_independent_of_the_graph() -> None:
    """结论的存放位置**不能**在图谱目录下 —— 图谱被重跑覆盖时结论要活着。"""
    root = Path("/tmp/x")
    assert review.review_path("com.demo", root) != review.index_path("com.demo", root)
    assert "artifacts" not in review.review_path("com.demo", root).parts


# -- 复核页渲染 -------------------------------------------------------------------

def test_render_html_inlines_screenshots_and_makes_titles_editable(tmp_path: Path) -> None:
    png = tmp_path / "artifacts" / "page_index" / "shots" / "a.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(_tiny_png())
    idx = _index([{**_page("k1", "被子搜索页"), "screenshot": "shots/a.png"}])
    html_text = review.render_html("com.demo", review.build_items(idx), tmp_path)

    assert 'src="data:image/jpeg;base64,' in html_text or 'src="data:image/png;base64,' in html_text, \
        "截图必须内联（相对路径在预览面板里会裂）"
    assert 'class="ti"' in html_text and 'data-key="k1"' in html_text, "标题要可编辑且带 page_key"
    assert 'value="被子搜索页"' in html_text, "输入框要预填当前标题"
    # 导出文件名是 JS 拼的（`PKG + '.review.json'`），不是字面量 —— 断言拼法本身
    assert "PKG + '.review.json'" in html_text, "导出文件名要带上包名"
    assert "mat review " in html_text and "--apply" in html_text, "页面上要给出下一步命令"


def test_render_html_survives_missing_screenshot(tmp_path: Path) -> None:
    """截图缺失不该让整页渲染失败 —— 用户仍然要能改标题。"""
    idx = _index([{**_page("k1", "被子搜索页"), "screenshot": "shots/没有这个文件.png"}])
    html_text = review.render_html("com.demo", review.build_items(idx), tmp_path)
    assert 'data-key="k1"' in html_text and "无截图" in html_text


def test_build_items_keeps_untitled_pages() -> None:
    """**没跑过 describe 的页也要列出来** —— 那正是最需要人工命名的情况。"""
    items = review.build_items(_index([_page("k1", ""), _page("k2", "首页")]))
    assert [i["page_key"] for i in items] == ["k1", "k2"]
    assert items[0]["title"] == "" and items[0]["activity"] == "ProductListActivity"


def _tiny_png(w: int = 24, h: int = 40) -> bytes:
    """自己写一张小 PNG —— 测试不该依赖系统里有没有某张图。"""
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

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
