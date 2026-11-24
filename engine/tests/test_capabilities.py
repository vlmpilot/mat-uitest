"""平台能力集合（`capabilities.py`）的契约测试。

守三件事（评审 #9 的核心诉求：不要再各说各话）：

1. **表本身自洽**：三个平台的键集合一致、值只有 True/"beta"/False；
2. **判断点不再分散**：CLI 的三个命令与两个 web_api 入口不许再写死
   `platform == "ios"` 这类能力判断——必须读 `capabilities`；
3. **文档与代码一致**：README 的平台能力表必须等于 `render_markdown()` 的输出。

第 3 条是关键：项目历史上多次出现"README 说一套、行为另一套"。把表格变成
生成物 + 断言，改能力忘了改文档就会红——这比"记得同步"可靠。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from insta_engine import capabilities as cap

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "engine" / "insta_engine"


def test_every_platform_declares_every_key():
    """缺键会让 `state()` 静默返回 False——那是"平台不支持"，不是"忘了填"。"""
    for platform, table in cap.CAPABILITIES.items():
        missing = set(cap.KEYS) - set(table)
        assert not missing, f"{platform} 缺能力声明：{sorted(missing)}"
        extra = set(table) - set(cap.KEYS)
        assert not extra, f"{platform} 声明了未登记的键：{sorted(extra)}"


def test_values_are_only_three_states():
    allowed = {True, False, "beta"}
    for platform, table in cap.CAPABILITIES.items():
        bad = {k: v for k, v in table.items() if v not in allowed}
        assert not bad, f"{platform} 出现非法状态（只允许 True/'beta'/False）：{bad}"


def test_supported_and_beta_are_distinct():
    """`beta` 必须放行（`supported` 为真）但能被 `beta_note` 提示出来。"""
    assert cap.supported("harmony", "explore") is True
    assert cap.is_beta("harmony", "explore") is True
    note = cap.beta_note("harmony", "explore")
    assert note and "beta" in note and "hilog" in note

    assert cap.supported("ios", "explore") is False
    assert cap.beta_note("ios", "explore") is None


def test_reason_is_specific_not_generic():
    """拒绝时给的是**具体原因**，不是"不支持"三个字。"""
    for platform, key in (("ios", "index"), ("ios", "explore"), ("harmony", "xpath")):
        text = cap.reason(platform, key)
        assert len(text) > 15, f"{platform}/{key} 的原因太笼统：{text}"
        assert "不支持" not in text or "shell" in text or "适配" in text or "请用" in text


def test_unknown_platform_falls_back_to_android():
    """历史行为：平台名写错时按 Android 处理（不能因为一个拼写错误全盘拒绝）。"""
    assert cap.state("windows", "run") is True
    assert cap.supported("", "run") is True


def test_require_raises_with_reason():
    with pytest.raises(ValueError, match="shell 通道"):
        cap.require("ios", "index")
    cap.require("android", "index")  # 不抛


def test_render_markdown_covers_all_platforms_and_states():
    md = cap.render_markdown()
    for platform in cap.CAPABILITIES:
        assert platform in md.splitlines()[0], "表头要列出所有平台"
    assert "⚠️ beta" in md and "✅" in md and "❌" in md


# -- 判断点不许再分散 -----------------------------------------------------------


def _function_body(path: Path, func: str) -> str:
    """粗切一个顶层函数的函数体（到下一个顶层 def 为止）。"""
    src = path.read_text(encoding="utf-8")
    start = src.find(f"def {func}(")
    assert start >= 0, f"{path.name} 里找不到 {func}"
    nxt = src.find("\ndef ", start + 1)
    return src[start:nxt if nxt > 0 else len(src)]


@pytest.mark.parametrize("func", ["cmd_index", "cmd_explore", "cmd_arrival"])
def test_cli_commands_use_capabilities_not_hardcoded_platform(func):
    """三个命令此前各写一句 `platform == "ios"`（评审 #9 的 5 处之二三）。"""
    body = _function_body(PKG / "__main__.py", func)
    assert 'platform == "ios"' not in body, f"{func} 里还有写死的平台判断"
    assert "capabilities.supported" in body, f"{func} 应读能力表"


@pytest.mark.parametrize("rel", ["web_api/indexing.py", "web_api/runs.py", "mcp_server.py"])
def test_web_and_mcp_use_capabilities(rel):
    src = (PKG / rel).read_text(encoding="utf-8")
    assert 'platform == "ios"' not in src, f"{rel} 里还有写死的平台判断（改读 capabilities）"


def test_orchestration_layer_has_no_platform_capability_checks():
    """编排层（runner/explore/page_index/arrivals）不许出现 `platform ==` 的能力判断。"""
    pattern = re.compile(r'platform\s*[!=]=\s*"(ios|android|harmony)"')
    for name in ("runner.py", "explore.py", "page_index.py", "arrivals.py"):
        src = (PKG / name).read_text(encoding="utf-8")
        hits = [ln for ln in src.splitlines()
                if pattern.search(ln) and "capabilities" not in ln]
        assert not hits, f"{name} 出现平台能力硬编码：{hits[:2]}"


# -- 文档与代码一致 -------------------------------------------------------------


def test_readme_platform_table_matches_capabilities():
    """README 的能力表必须逐行等于 `render_markdown()`。

    项目反复吃过"文档说一套、行为另一套"的亏；把表格变成生成物 + 断言，
    改能力忘了改文档就红。
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for line in cap.render_markdown().splitlines():
        assert line in readme, f"README 的能力表与 capabilities 不一致，缺这一行：{line}"


def test_capability_keys_have_human_labels():
    """每个键都要有人读说明（README 表格与报错文案都从这里取）。"""
    for key in cap.KEYS:
        assert cap.KEYS[key].strip(), f"{key} 缺说明"
