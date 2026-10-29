"""mat finetune:数据抽取/门槛闸/JSONL 形状/use 配置(L2 工具链)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine.finetune import MIN_PAIRS, collect_pairs, export_dataset, write_use_config


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


def test_collect_pairs_filters_and_dedups(tmp_path):
    _mk_run(tmp_path, "c1", "com.x", status="passed", nl="打开设置")
    _mk_run(tmp_path, "c1", "com.x", status="passed", nl="打开设置")       # 同用例多轮 → 去重
    _mk_run(tmp_path, "c2", "com.x", status="failed", nl="失败的不学")
    _mk_run(tmp_path, "c3", "com.y", status="passed", nl="另一个 App")
    r = collect_pairs(tmp_path, "com.x")
    assert len(r["pairs"]) == 1, "同 caseId 去重 + failed 剔除"
    assert r["pairs"][0]["messages"][0]["content"].startswith("打开设置")
    assert r["pairs"][0]["messages"][1]["content"].startswith("{")


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


def test_export_jsonl_shape_and_stats(tmp_path):
    _mk_run(tmp_path, "c1", "com.x", nl="打开设置")
    code, _ = None, None
    stats = export_dataset(tmp_path, "com.x", tmp_path / "dataset")
    assert stats["pairs"] == 1 and stats["enough"] is False  # 1 < 200
    lines = [json.loads(l) for l in
             (tmp_path / "dataset" / "train.jsonl").read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0][0]["role"] == "user" and lines[0][1]["role"] == "assistant"
    assert "打开设置" in lines[0][0]["content"]


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
