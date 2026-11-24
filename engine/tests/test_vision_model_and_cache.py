"""视觉兜底的「按模型选 prompt / 解析」与「缓存三态」（评审 #12）。

## 这一组测什么

1. **模型族识别**：只认输出格式已确认的（UI-TARS）。GUI-Owl 之类**故意不映射**——
   没见过真实输出就写解析器，只会造出"看起来支持"的假象。
2. **box 解析**：UI-TARS 输出 `<|box_start|>(x1,y1),(x2,y2)<|box_end|>`，坐标是 0~1000
   千分比；取 box 中心换算成比例，走既有的比例分支（不受屏幕尺寸影响）。
3. **缓存三态**：`memory`（默认）/ `disk`（CI 复用）/ `off`，且**换模型不混用**。

⚠️ 没有 UI-TARS 的额度，所以这里用的是**按官方格式构造的字符串**——
验证的是解析与选择逻辑，不是"该模型在真机上的准确率"。后者要用 `mat grounding-eval`。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from insta_engine import vision


# -- 模型族 ---------------------------------------------------------------------


@pytest.mark.parametrize("model,expected", [
    ("UI-TARS-1.5-7B", "uitars"),
    ("ui-tars-7b-dpo", "uitars"),
    ("qwen3-vl-plus", "generic"),
    ("gpt-4o", "generic"),
    ("", "generic"),
    ("gui-owl-7b", "generic"),   # 未确认输出格式 ⇒ 不假装支持
])
def test_model_family(model, expected):
    assert vision.model_family(model) == expected


def test_prompt_name_falls_back_to_generic_when_no_template(monkeypatch):
    """专用模板不存在时必须退回通用，而不是拿一个不存在的路径去读。"""
    monkeypatch.setattr("insta_engine.promptstore.resolve", lambda name, ws=None: None)
    assert vision.prompt_name("UI-TARS-1.5-7B") == "vision-locate"
    assert vision.prompt_name("qwen3-vl-plus") == "vision-locate"


# -- box 解析 -------------------------------------------------------------------


def test_uitars_box_is_parsed_to_ratio_center():
    fields = vision.extract_fields(
        '<|box_start|>(100,200),(300,400)<|box_end|>\n'
        '{"found": true, "confidence": 0.9, "desc": "登录按钮"}')
    assert fields["box_permille"] == [100.0, 200.0, 300.0, 400.0]
    assert fields["found"] is True
    assert fields["x"] == pytest.approx(0.2)   # 中心 (200,300)/1000
    assert fields["y"] == pytest.approx(0.3)


def test_uitars_all_zero_box_means_not_found():
    """prompt 约定：找不到时输出全零 box。不能把它当成"点在左上角"。"""
    fields = vision.extract_fields(
        '<|box_start|>(0,0),(0,0)<|box_end|>\n{"found": false, "confidence": 0.1}')
    assert fields["found"] is False
    assert fields["x"] is None and fields["y"] is None


def test_box_end_to_end_maps_to_pixels():
    """box → 像素：1000×2000 的图上，box 中心 (200,300)千分比 = (200, 600) 像素。"""
    buf = io.BytesIO()
    Image.new("RGB", (1000, 2000), "white").save(buf, format="PNG")
    png = buf.getvalue()

    fields = vision.extract_fields(
        '<|box_start|>(100,200),(300,400)<|box_end|>'
        '{"found": true, "confidence": 0.9, "desc": "登录"}')
    pos = vision.parse_vision_response(fields, png, tree_evidence=True, target="登录")
    assert pos == (200, 600)


def test_box_without_confidence_is_rejected_with_hint():
    """GUI 专用模型若没给 confidence，按红线拒绝——但原因要说清"缺什么"。

    红线不能为"支持某模型"让步：没有置信度就没有拒绝的依据，盲点的代价远大于漏点。
    """
    buf = io.BytesIO()
    Image.new("RGB", (1000, 2000), "white").save(buf, format="PNG")
    fields = vision.extract_fields('<|box_start|>(100,200),(300,400)<|box_end|>{"found": true}')
    assert vision.parse_vision_response(fields, buf.getvalue(), tree_evidence=True) is None
    reason = vision.last_result()["reason"]
    assert "box" in reason and "confidence" in reason


# -- 缓存三态 -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "_disk_override", tmp_path / "vision_cache.json")
    monkeypatch.setenv("GEN_VL_MODEL", "qwen3-vl-plus")
    vision.reset_cache(mode="memory")
    yield
    monkeypatch.setattr(vision, "_disk_override", None)
    vision.reset_cache(mode="memory")


def test_memory_mode_does_not_write_disk(tmp_path):
    vision._cache_put("k1", (10, 20))
    assert vision._cache_get("k1") == (10, 20)
    assert not (tmp_path / "vision_cache.json").exists()


def test_off_mode_disables_cache():
    vision.reset_cache(mode="off")
    vision._cache_put("k1", (10, 20))
    assert vision._cache_get("k1") is None


def test_disk_mode_roundtrip_survives_memory_clear(tmp_path):
    """落盘的意义：进程重启后同一屏同一目标不用再付费。"""
    vision.reset_cache(mode="disk")
    vision._cache_put("tree:hash:登录", (540, 1093))
    assert (tmp_path / "vision_cache.json").exists()

    # 模拟进程重启：清内存 + 清落盘缓存对象
    vision._CACHE.clear()
    vision._disk = None
    assert vision._cache_get("tree:hash:登录") == (540, 1093)


def test_disk_cache_is_not_shared_across_models(tmp_path):
    """换模型不混用：不同模型的偏差不同，坐标不能跨模型复用。"""
    vision.reset_cache(mode="disk")
    vision._cache_put("k1", (100, 200))

    vision._CACHE.clear()
    vision._disk = None
    assert vision._cache_get("k1") == (100, 200)

    import os
    os.environ["GEN_VL_MODEL"] = "UI-TARS-1.5-7B"
    vision._CACHE.clear()
    vision._disk = None
    assert vision._cache_get("k1") is None, "换了模型就不能复用旧坐标"


def test_corrupt_disk_cache_is_tolerated(tmp_path):
    vision.reset_cache(mode="disk")
    (tmp_path / "vision_cache.json").write_text("{坏文件", encoding="utf-8")
    vision._disk = None
    assert vision._cache_get("k1") is None          # 不抛
    vision._cache_put("k1", (1, 2))                 # 还能正常写回
    assert vision._cache_get("k1") == (1, 2)
