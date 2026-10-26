"""图谱准备度（readiness）测试（2026-10-17）。

只测**口径与建议规则**，不测实现细节：
- 「已复核」的唯一判据是 `title_source == "manual"`（describe/merge 都不覆盖）；
- 无图谱 ⇒ 建议「先建图谱」；有图谱但有未复核 ⇒ 建议「复核」；全部复核完 ⇒ **不给建议**（别刷无意义提示）；
- `/api/readiness` 只读（不写图谱）、非法包名拒绝。

为什么这些测试重要：这组提示是「不做硬闸门」方案的全部依托（见 MAT-图谱准入门槛评估-2026-10-17.md）
——提示错了不会拦人，但会误导用户把时间花在没收益的地方。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from insta_engine import readiness as rd  # noqa: E402
from insta_engine.web_api.readiness import api_readiness  # noqa: E402


def _write_index(root: Path, package: str, pages: list[dict]) -> None:
    d = root / "artifacts" / "page_index"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{package}.json").write_text(
        json.dumps({"version": 2, "package": package, "pages": pages}, ensure_ascii=False),
        encoding="utf-8")


def _page(key: str, title_source: str = "heuristic") -> dict:
    return {"page_key": key, "title": f"页{key}", "title_source": title_source,
            "controls": [{"text": "确定", "clickable": True}]}


# -- 口径 ---------------------------------------------------------------------


def test_no_index_reports_absent(tmp_path):
    r = rd.readiness(tmp_path, "com.x.app")
    assert r["has_index"] is False and r["pages"] == 0
    h = rd.hint(r)
    assert h and "还没有" in h and "mat index com.x.app" in h, "无图谱时应建议先建图谱"


def test_reviewed_counts_only_manual_title_source(tmp_path):
    """「已复核」= `title_source == "manual"` —— ai/heuristic/空都不算。"""
    _write_index(tmp_path, "com.x.app", [
        _page("a", "manual"), _page("b", "ai"), _page("c", "heuristic"),
        {"page_key": "d", "title": "页d"},                       # 无 title_source 字段
    ])
    r = rd.readiness(tmp_path, "com.x.app")
    assert r["pages"] == 4 and r["reviewed"] == 1 and r["unreviewed"] == 3


def test_hint_recommends_review_when_unreviewed(tmp_path):
    _write_index(tmp_path, "com.x.app", [_page("a", "manual"), _page("b", "ai")])
    h = rd.hint(rd.readiness(tmp_path, "com.x.app"))
    assert h and "1 页未复核" in h and "mat review com.x.app" in h


def test_no_hint_when_all_reviewed(tmp_path):
    """全部复核完 ⇒ 返回 None（调用方据此不显示提示）。"""
    _write_index(tmp_path, "com.x.app", [_page("a", "manual"), _page("b", "manual")])
    r = rd.readiness(tmp_path, "com.x.app")
    assert rd.hint(r) is None
    assert "已复核 2 页" in rd.summary(r)


def test_broken_index_degrades_to_absent(tmp_path):
    """图谱损坏 ⇒ 当没有处理（load_index 的既有口径），不抛异常。"""
    d = tmp_path / "artifacts" / "page_index"
    d.mkdir(parents=True)
    (d / "com.x.app.json").write_text("{ 这不是 JSON", encoding="utf-8")
    r = rd.readiness(tmp_path, "com.x.app")
    assert r["has_index"] is False


# -- API ---------------------------------------------------------------------


def test_api_readiness_readonly_and_shape(tmp_path):
    _write_index(tmp_path, "com.x.app", [_page("a", "ai")])
    before = (tmp_path / "artifacts" / "page_index" / "com.x.app.json").read_bytes()

    code, obj = api_readiness(tmp_path, "com.x.app")

    assert code == 200
    assert obj["pages"] == 1 and obj["unreviewed"] == 1
    assert obj["text"] and obj["hint"] and obj["explore_tip"]
    # 控制台口径（2026-10-25）：界面用户不该被引导去终端——提示给页面名，不给 mat 命令
    assert "mat " not in obj["hint"] and "页面复核" in obj["hint"]
    # 2026-10-26：判据内容保留但「甜点判据」术语不进产品文案
    assert "甜点" not in obj["explore_tip"] and "新增页面数" in obj["explore_tip"]
    # 只读：文件逐字节没变
    assert (tmp_path / "artifacts" / "page_index" / "com.x.app.json").read_bytes() == before


@pytest.mark.parametrize("pkg", ["", "../etc/passwd", "a/b"])
def test_api_readiness_rejects_bad_package(tmp_path, pkg):
    assert api_readiness(tmp_path, pkg)[0] == 400


def test_explore_tip_is_judgement_rule_not_magic_number():
    """探索建议必须是**判据**（边际新增是否还在涨），不能是编出来的固定时长；不带术语。"""
    assert "新增页面数" in rd.EXPLORE_TIP
    assert "收手" in rd.EXPLORE_TIP
    assert "复核页名" in rd.EXPLORE_TIP
    assert "甜点" not in rd.EXPLORE_TIP
