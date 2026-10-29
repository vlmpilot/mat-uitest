"""mat finetune:数据抽取/切分/门槛闸/JSONL 形状/训练解释器/评测口径(L2 工具链)。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from insta_engine.finetune import (
    MIN_PAIRS,
    collect_pairs,
    eval_adapter,
    export_dataset,
    load_holdout,
    split_pairs,
    train,
    write_use_config,
)


def _mk_run(root: Path, cid: str, pkg: str, status: str = "passed",
            nl: str | None = "打开设置回到桌面") -> None:
    d = {"run": {"status": status},
         "case": {"caseId": cid, "name": f"用例{cid}", "target": {"package": pkg},
                  "steps": [{"id": "s1", "action": "launch",
                             "params": {"package": pkg}}],
                  "meta": ({"nl": nl} if nl else {})}}
    out = root / "artifacts" / f"r-{cid}" / "run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _mk_pairs(n: int) -> list[dict]:
    return [{"messages": [{"role": "user", "content": f"需求{c}"},
                          {"role": "assistant", "content": "{}"}],
             "meta": {"case_id": f"c{c:04d}"}} for c in range(n)]


def test_collect_pairs_filters_and_dedups(tmp_path):
    _mk_run(tmp_path, "c1", "com.x", status="passed", nl="打开设置")
    _mk_run(tmp_path, "c1", "com.x", status="passed", nl="打开设置")       # 同用例多轮 → 去重
    _mk_run(tmp_path, "c2", "com.x", status="failed", nl="失败的不学")
    _mk_run(tmp_path, "c3", "com.y", status="passed", nl="另一个 App")
    r = collect_pairs(tmp_path, "com.x")
    assert len(r["pairs"]) == 1, "同 caseId 去重 + failed 剔除"
    assert r["pairs"][0]["messages"][0]["content"].startswith("打开设置")
    assert r["pairs"][0]["messages"][1]["content"].startswith("{")


def test_collect_pairs_label_excludes_meta_nl(tmp_path):
    """review P0-4：assistant 标签不能含 meta.nl（否则模型学会复述输入需求）。"""
    _mk_run(tmp_path, "c1", "com.x", nl="打开设置回到桌面")
    label = collect_pairs(tmp_path)["pairs"][0]["messages"][1]["content"]
    assert "打开设置回到桌面" not in label
    assert "meta" not in json.loads(label)


def test_collect_pairs_counts_no_nl(tmp_path):
    """历史存量(无 meta.nl)如实剔除并计数——不冒充训练对。"""
    _mk_run(tmp_path, "c1", "com.x", status="passed", nl=None)
    r = collect_pairs(tmp_path, "com.x")
    assert r["pairs"] == [] and r["no_nl"] == 1


def test_collect_pairs_app_filter(tmp_path):
    _mk_run(tmp_path, "c1", "com.x", nl="a")
    _mk_run(tmp_path, "c2", "com.y", nl="b")
    assert len(collect_pairs(tmp_path, "com.x")["pairs"]) == 1
    assert len(collect_pairs(tmp_path)["pairs"]) == 2


def test_split_is_disjoint_and_capped():
    """review P0-2：留出集与 train/valid 互斥，且不超过 30 条。"""
    split = split_pairs(_mk_pairs(200))
    train_ids = {p["meta"]["case_id"] for p in split["train"]}
    valid_ids = {p["meta"]["case_id"] for p in split["valid"]}
    holdout_ids = {p["meta"]["case_id"] for p in split["holdout"]}
    assert len(split["holdout"]) == 30
    assert len(split["train"]) + len(split["valid"]) == 170
    assert not (train_ids & holdout_ids) and not (valid_ids & holdout_ids)
    assert not (train_ids & valid_ids)


def test_export_writes_mlx_chat_shape_and_three_splits(tmp_path):
    """review P0-1：每行必须是 {"messages": [...]}（mlx_lm chat 格式），不是裸数组。"""
    for i in range(6):
        _mk_run(tmp_path, f"c{i}", "com.x", nl=f"需求{i}")
    stats = export_dataset(tmp_path, "com.x", tmp_path / "dataset")
    assert stats["pairs"] == 6 and stats["train_pairs"] == 5
    assert stats["holdout_pairs"] == 1 and stats["enough"] is False
    for name in ("train", "valid", "holdout"):
        assert (tmp_path / "dataset" / f"{name}.jsonl").exists()
    lines = [json.loads(line) for line in
             (tmp_path / "dataset" / "train.jsonl").read_text().splitlines() if line.strip()]
    assert len(lines) == 5
    assert isinstance(lines[0], dict) and lines[0]["messages"][0]["role"] == "user"
    assert lines[0]["messages"][1]["role"] == "assistant"


def test_train_rejects_below_threshold(tmp_path):
    """门槛闸：可训练对不足时**不启动子进程**，直接如实拒绝。"""
    (tmp_path / "train.jsonl").write_text('{"messages": []}\n' * 10, encoding="utf-8")
    with mock.patch("insta_engine.finetune.subprocess.run") as run:
        r = train(tmp_path, tmp_path, "some/base")
    assert r["ok"] is False and "不足" in r["error"]
    run.assert_not_called()


def test_train_uses_current_interpreter_and_hints_missing_mlx(tmp_path):
    """review P1-5：默认用当前解释器（而非 PATH 的 python3），缺 mlx 给安装指引。"""
    (tmp_path / "train.jsonl").write_text('{"messages": []}\n' * MIN_PAIRS, encoding="utf-8")
    proc = mock.Mock(returncode=1, stdout="",
                     stderr="/usr/bin/python3: No module named mlx_lm")
    with mock.patch("insta_engine.finetune.subprocess.run", return_value=proc) as run:
        r = train(tmp_path, tmp_path, "some/base")
    assert run.call_args.args[0][0] == sys.executable
    assert r["ok"] is False and "pip install mlx-lm" in r["error"]


def test_load_holdout_missing_file_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError, match="先运行"):
        load_holdout(tmp_path)


def test_eval_uses_gen_client_and_writes_report(tmp_path, monkeypatch):
    """review P0-3/P1-7：eval 复用 GenClient（gen 同口径）并落评测报告。"""
    from insta_engine.dsl import TestCase

    calls: list[tuple[str, dict]] = []

    class FakeGen:
        def __init__(self, base_url, api_key, model, prompt_path=None, prompt_mode="local"):
            self.base_url = base_url

        def generate(self, nl, app_info=None, index_dir=None):
            calls.append((nl, app_info or {}))
            return TestCase.model_validate({
                "caseId": "x", "name": "n", "target": {"package": "com.x"},
                "steps": [{"id": "s1", "action": "wait", "params": {"ms": 1}},
                          {"id": "s2", "action": "wait", "params": {"ms": 1}}],
            })

    monkeypatch.setattr("insta_engine.ai_client.GenClient", FakeGen)
    holdout = [{"messages": [
        {"role": "user", "content": "打开设置\n\nApp 信息: {}"},
        {"role": "assistant", "content": json.dumps({"steps": [{"id": "s1"}, {"id": "s2"}]})},
    ], "meta": {"nl": "打开设置", "app_info": {"package": "com.x"}}}]
    report = tmp_path / "dataset" / "eval-report.json"
    r = eval_adapter(tmp_path, "adapters/x", "base", holdout, report_path=report)
    assert r["ok"] and r["auto_pass"] == 1.0 and r["near"] == 1
    assert calls == [("打开设置", {"package": "com.x"})], "必须走 gen 的 nl/app_info 入口"
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["schema"] == "mat.finetune.eval/1" and saved["rows"][0]["verdict"] == "near"


def test_write_use_config_overrides_and_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEN_BASE_URL=https://old\nGEN_API_KEY=sk-x\nOTHER=1\n", encoding="utf-8")
    p = write_use_config(tmp_path, "adapters/xhs", "mlx-community/Qwen3-9B")
    assert p == env
    text = env.read_text(encoding="utf-8")
    assert "GEN_BASE_URL=http://127.0.0.1:8080" in text
    assert "GEN_PROMPT_MODE=local" in text
    assert "GEN_API_KEY=local" in text
    assert "OTHER=1" in text and "https://old" not in text


def test_min_pairs_gate_is_honest():
    """门槛常量与文档一致(≥200)——工具链不因数据稀少而偷偷降低要求。"""
    assert MIN_PAIRS == 200
