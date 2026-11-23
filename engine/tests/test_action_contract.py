"""公开 composite action 的契约自检（纯静态，不需要 GitHub 环境）。

action.yml 会随公开仓分发、被别人直接 uses 到自己的 workflow，错误接线在
本仓的离线门禁里没有任何东西能发现。这里锁住四类已经出现过的回归：
输入内插进 shell、outputs 指向不写输出的 step、用例失败时证据不收集、
安装版本不钉死。纯文本检查，公开仓 CI 也能跑。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / "action.yml"
EXAMPLE = ROOT / "examples" / "ci" / "mat-pr.yml"

pytestmark = pytest.mark.skipif(not ACTION.exists(), reason="源码树里没有 action.yml")


def _shell(text: str) -> str:
    """取所有 run 块里的 shell 行；单行 run 也收集成一条违规提示。"""
    block: list[str] = []
    single: list[str] = []
    collecting = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "run: |":
            collecting = True
            continue
        if collecting:
            if line.startswith("        "):
                block.append(line)
                continue
            if stripped == "":
                continue
            collecting = False
        if stripped.startswith("run:") and stripped != "run: |":
            single.append(f"单行 run 未用块形式: {stripped}")
    return "\n".join(block + single)


def test_inputs_never_interpolated_into_shell():
    shell = _shell(ACTION.read_text(encoding="utf-8"))
    assert "单行 run 未用块形式" not in shell
    assert "${{ inputs." not in shell
    assert "${{ github." not in shell


def test_outputs_point_at_the_step_that_writes_them():
    text = ACTION.read_text(encoding="utf-8")
    assert "steps.run.outputs." not in text
    names = set(re.findall(r"steps\.summary\.outputs\.([\w-]+)", text))
    assert {"junit", "report", "passed", "total", "summary"} <= names
    shell = _shell(text)
    for name in names:
        assert re.search(rf'echo "{re.escape(name)}=', shell), f"summary 没写 {name}"


def test_evidence_steps_survive_failed_runs():
    text = ACTION.read_text(encoding="utf-8")
    for step in ("摘要（JSON + Markdown）", "上传报告"):
        tail = text[text.index(step):text.index(step) + 700]
        assert "if: always()" in tail, f"{step} 缺少失败保护"


def test_install_is_pinned_and_example_output_exists():
    text = ACTION.read_text(encoding="utf-8")
    assert "mat-uitest==" in _shell(text)
    assert "artifact-name:" in text
    if EXAMPLE.exists():
        assert "steps.mat.outputs.artifact-name" in EXAMPLE.read_text(encoding="utf-8")


def test_default_version_follows_engine_version():
    """钉死的版本必须跟着版本单源走，否则发新版后 action 会静默装旧包。"""
    from insta_engine import __version__

    text = ACTION.read_text(encoding="utf-8")
    segment = text.split("  version:", 1)[1].split("outputs:", 1)[0]
    assert f'default: "{__version__}"' in segment
