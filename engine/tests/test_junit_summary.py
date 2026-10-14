"""junit_summary 测试：与 render_junit_xml 同源，解析/Markdown/环境抖动标注。"""

import json
from pathlib import Path

import pytest

from insta_engine.junit_summary import markdown, summarize
from insta_engine.report import render_junit_xml
from tests.test_engine import _fake_run


@pytest.fixture
def junit(tmp_path: Path) -> Path:
    out = tmp_path / "junit.xml"
    out.write_text(render_junit_xml(_fake_run()), encoding="utf-8")
    return out


def test_summarize_counts_and_categories(junit: Path):
    s = summarize(junit)
    assert s["total"] == len(s["cases"]) > 0
    assert s["passed"] + s["failed"] == s["total"]
    failed = [c for c in s["cases"] if c["status"] == "failed"]
    assert failed and all(c.get("category") for c in failed)  # 分类随 JUnit 带出


def test_markdown_lists_failures_first(junit: Path):
    md = markdown(summarize(junit))
    assert "通过" in md and "失败明细" in md
    assert md.index("失败明细") < md.rindex("`")  # 失败行在文档里


def test_env_failure_is_annotated(junit: Path):
    s = summarize(junit)
    md = markdown(s)
    env = [c for c in s["cases"] if c.get("category") == "env-failure"]
    if env:
        assert "不计入产品失败" in md


def test_json_output_roundtrip(junit: Path):
    s = summarize(junit)
    assert json.loads(json.dumps(s))["total"] == s["total"]
