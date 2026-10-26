"""产物布局约定（台账 C33 / 评审 P1-6，2026-10-24）：落盘的 run.json 必能被两端发现。

CLI 套件/数据驱动的产物在**两层**目录（`artifacts/<套件>/<NN>_<stem>/`、
`artifacts/<stem>-ds/<组名>/`；stages 套件三层），而 `mat export`（PIPL 导出）
与配置台 `list_runs` 此前都只 glob 一层 ⇒ **导出漏文件 + CLI 跑的套件在配置台
不可见**。这条约定测试把「布局 × 发现机制」钉死：任一端改布局或改发现深度，
另一端立刻红——不再靠"记得同步"。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

# 真实布局清单：(artifacts 下的相对目录, 语义)——覆盖一层/两层/三层全部形态
LAYOUTS = [
    ("single", "单条用例（一层）"),
    ("smoke/01_login", "平铺套件条目（两层）"),
    ("smoke/02_pay", "平铺套件条目（两层）"),
    ("checkout-ds/组A", "数据驱动组（两层）"),
    ("big/初始化/01_a", "stages 套件条目（三层）"),
]


def _make_tree(root: Path) -> None:
    for rel, _note in LAYOUTS:
        d = root / "artifacts" / rel
        (d / "screenshots").mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps({
            "finished_at": "2026-10-24T10:00:00+00:00",
            "case": {"target": {"package": "com.demo"}},
            "run": {"case_id": rel.replace("/", "_"), "case_name": rel,
                    "status": "passed", "step_success_rate": 1.0,
                    "failed_categories": {}},
        }, ensure_ascii=False), encoding="utf-8")
        (d / "report.html").write_text("<html>ok</html>", encoding="utf-8")
        (d / "screenshots" / "step_01.png").write_bytes(b"\x89PNG fake")


def test_list_runs_discovers_every_layout(tmp_path):
    """配置台 list_runs：rglob 必须发现一层/两层/三层全部 run.json，
    `dir` 为 artifacts 相对 posix 路径（一层时与旧叶子名相同 ⇒ 兼容）。"""
    from insta_engine.web_api.workspace import list_runs

    _make_tree(tmp_path)
    runs = list_runs(tmp_path)
    got = {r["dir"] for r in runs}
    want = {rel for rel, _ in LAYOUTS}
    missing = want - got
    assert not missing, f"list_runs 漏掉布局: {missing}（发现 {sorted(got)}）"
    # 一层布局的 dir 与旧值（叶子名）相同——前端/接口向后兼容
    assert "single" in got


def test_cmd_export_includes_every_layout(tmp_path, monkeypatch):
    """PIPL 导出（mat export --media）：rglob 后套件/数据驱动/stages 的
    run.json、report.html、截图全部进包——此前一层 glob 全部漏掉。"""
    import argparse

    from insta_engine.__main__ import cmd_export

    _make_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "export.zip"
    rc = cmd_export(argparse.Namespace(out=str(out), format=None, media=True))
    assert rc == 0
    names = set(zipfile.ZipFile(out).namelist())
    for rel, _note in LAYOUTS:
        assert f"artifacts/{rel}/run.json" in names, f"导出漏 run.json: {rel}"
        assert f"artifacts/{rel}/report.html" in names, f"导出漏 report: {rel}"
        assert f"artifacts/{rel}/screenshots/step_01.png" in names, f"导出漏截图: {rel}"
