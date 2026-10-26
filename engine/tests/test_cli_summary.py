"""`mat summary` 子命令（2026-10-25，marketplace skill 的 agent 读数入口）。

为什么 CLI 化是刚需：`python -m insta_engine.run_summary` 要求**当前解释器**装着
insta_engine——uv tool / pipx 的隔离 venv（skill 推荐装法）下必挂。

退出码语义（2026-10-25 评审 P2-1/P2-3 钉死，CI 依赖）：
0=用例通过 / 1=用例未通过（failed/skipped/cancelled）/ 2=用法或产物不可判读。
任何输入都不得吐 traceback——调用方是 agent/CI，栈只会被当成「工具崩了」。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from insta_engine.__main__ import cmd_summary, main


def _ns(**kw):
    base = {"run_dir": None, "self_test": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _make_run(tmp_path: Path, status: str = "passed") -> Path:
    run_dir = tmp_path / "case_x"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "device_serial": "SER",
        "case": {"name": "摘要用例"},
        "run": {"case_id": "case_x", "case_name": "摘要用例", "status": status,
                "step_success_rate": 1.0 if status == "passed" else 0.5,
                "duration_ms": 1000, "failed_categories": {},
                "steps": [{"step_id": "s1", "status": status}]},
    }, ensure_ascii=False), encoding="utf-8")
    return run_dir


def test_summary_self_test(capsys):
    assert cmd_summary(_ns(self_test=True)) == 0
    assert "self-test 通过" in capsys.readouterr().out


def test_summary_passed_exits_zero(tmp_path, capsys):
    run_dir = _make_run(tmp_path, "passed")
    assert cmd_summary(_ns(run_dir=str(run_dir))) == 0
    out = capsys.readouterr().out
    assert "摘要用例" in out and "passed" in out and "100%" in out


def test_summary_failed_exits_one(tmp_path, capsys):
    run_dir = _make_run(tmp_path, "failed")
    assert cmd_summary(_ns(run_dir=str(run_dir))) == 1


# -- 失败路径硬化（P2-1）：任何输入不吐 traceback，退出码 2=不可判读 -------------

def test_summary_no_args_prints_usage_exits_two(capsys):
    assert cmd_summary(_ns()) == 2
    err = capsys.readouterr().err
    assert "用法" in err and "Traceback" not in err


def test_summary_missing_dir_is_actionable(tmp_path, capsys):
    assert cmd_summary(_ns(run_dir=str(tmp_path / "ghost"))) == 2
    err = capsys.readouterr().err
    assert "不存在" in err and "run.json" in err and "Traceback" not in err


def test_summary_corrupt_json_names_the_file(tmp_path, capsys):
    run_dir = tmp_path / "bad"
    run_dir.mkdir()
    (run_dir / "run.json").write_text('{"case": ', encoding="utf-8")
    assert cmd_summary(_ns(run_dir=str(run_dir))) == 2
    err = capsys.readouterr().err
    assert str(run_dir) in err and "不是合法 JSON" in err and "Traceback" not in err


def test_summary_non_object_run_json(tmp_path, capsys):
    run_dir = tmp_path / "arr"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("[]", encoding="utf-8")
    assert cmd_summary(_ns(run_dir=str(run_dir))) == 2
    err = capsys.readouterr().err
    assert "应是对象" in err and "重跑" in err and "Traceback" not in err


def test_summary_unknown_status_exits_two_not_one(tmp_path, capsys):
    """P2-3：unknown/缺失 status = 产物不可判读（2），别和「用例未通过」（1）混同。"""
    run_dir = tmp_path / "weird"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "run": {"status": "unknown", "steps": []}}, ensure_ascii=False), encoding="utf-8")
    assert cmd_summary(_ns(run_dir=str(run_dir))) == 2


# -- parser 端到端（P3-1）：直调 cmd_summary 盖不住「注册写错」这层 -----------------

def test_summary_registered_in_cli_parser(tmp_path, capsys, monkeypatch):
    """走 main(["summary", ...]) 全链——若 subparser 注册/dest 写错，这里立刻红。"""
    monkeypatch.setattr("insta_engine.__main__._load_env_file", lambda *a, **k: None)
    run_dir = _make_run(tmp_path, "passed")
    with pytest.raises(SystemExit) as ei:
        main(["summary", str(run_dir)])
    assert ei.value.code == 0
    assert "摘要用例" in capsys.readouterr().out


def test_summary_no_args_via_cli_parser(capsys, monkeypatch):
    monkeypatch.setattr("insta_engine.__main__._load_env_file", lambda *a, **k: None)
    with pytest.raises(SystemExit) as ei:
        main(["summary"])
    assert ei.value.code == 2
    assert "用法" in capsys.readouterr().err
