"""评测台账（`scripts/bench_ledger.py`）的契约测试。

它守的是两条纪律：
1. **数字只能从 `run.json` 读**——台账不接受手填，否则就是"我说"；
2. **产物被清理时保留记录**，只报告 —— 历史成绩不该因为清磁盘而消失。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载 scripts/bench_ledger.py（它不在包里）。"""
    path = Path(__file__).resolve().parents[2] / "scripts" / "bench_ledger.py"
    spec = importlib.util.spec_from_file_location("bench_ledger", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ledger_mod(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "results" / "ledger.jsonl")
    return mod


def _write_run(art: Path, idx: int, case_id: str, status: str, steps: list[dict]) -> None:
    d = art / f"{idx:02d}_{case_id}"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "finished_at": "2026-10-20T12:00:00",
        "device_serial": "SERIAL",
        "platform": "ios",
        "device": {"model": "iPhone 17", "os_version": "26.5"},
        "run": {"case_id": case_id, "case_name": f"用例 {case_id}", "status": status,
                "duration_ms": 1000, "steps": steps},
    }, ensure_ascii=False), encoding="utf-8")


def test_summarize_counts_from_run_json_only(ledger_mod, tmp_path):
    art = tmp_path / "bench" / "arm"
    _write_run(art, 1, "c1", "passed", [{"status": "passed"}, {"status": "passed"}])
    _write_run(art, 2, "c2", "failed", [
        {"status": "passed"},
        {"status": "failed", "error": "AssertionError: 断言失败: 控件应存在: 详情：项目 1\n第二行"},
    ])

    s = ledger_mod.summarize_arm(art)
    assert (s["cases"], s["passed"], s["failed"]) == (2, 1, 1)
    assert (s["steps_passed"], s["steps_total"]) == (3, 4)
    assert s["device"] == "iPhone 17 · 26.5"
    # 只留错误首行：台账要小，细节看产物
    assert s["failed_cases"][0]["error"] == "AssertionError: 断言失败: 控件应存在: 详情：项目 1"


def test_record_is_idempotent_and_rejects_missing_artifacts(ledger_mod, tmp_path, capsys):
    art = tmp_path / "bench" / "arm"
    _write_run(art, 1, "c1", "passed", [{"status": "passed"}])

    args = type("A", (), {"benchmark": str(tmp_path / "bench" / "probes.json"),
                          "artifacts": [str(art)], "arm": None, "date": "2026-10-20",
                          "note": "", "force": False})()
    (tmp_path / "bench" / "probes.json").write_text("{}", encoding="utf-8")

    assert ledger_mod.cmd_record(args) == 0
    assert ledger_mod.cmd_record(args) == 0          # 同键跳过，不重复写
    rows = [json.loads(x) for x in
            ledger_mod.LEDGER.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 1 and rows[0]["benchmark"] == "probes"

    args.artifacts = [str(tmp_path / "nope")]
    with pytest.raises(SystemExit):
        ledger_mod.cmd_record(args)                  # 产物不存在 ⇒ 直接拒绝，不写空记录


def test_verify_keeps_record_when_artifacts_gone(ledger_mod, tmp_path, capsys):
    art = tmp_path / "bench" / "arm"
    _write_run(art, 1, "c1", "passed", [{"status": "passed"}])
    ledger_mod.LEDGER.parent.mkdir(parents=True)
    ledger_mod.LEDGER.write_text(json.dumps({
        "date": "2026-10-20", "benchmark": "p", "arm": "arm", "note": "",
        "engine": "0.7.0", "artifacts": "bench/arm", "cases": 1, "passed": 1,
        "failed": 0, "steps_total": 1, "steps_passed": 1, "duration_s": 1.0,
        "platform": "ios", "device": "iPhone 17 · 26.5", "serial": "SERIAL",
        "failed_cases": [],
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    assert ledger_mod.cmd_verify(type("A", (), {})()) == 0

    # 产物被清理：记录保留、只报告（历史成绩不该因清磁盘消失）
    import shutil
    shutil.rmtree(art)
    assert ledger_mod.cmd_verify(type("A", (), {})()) == 1
    assert "产物已不在" in capsys.readouterr().out
    assert ledger_mod.LEDGER.read_text(encoding="utf-8").strip()
