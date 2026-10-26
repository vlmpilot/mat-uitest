"""产物写盘与自愈提案读取的可行动降级（MAT 评估 2026-10-24 P1-1 / P1-2）。

- runner：run.json 写不进去（磁盘满/权限/占位）**不得**让整个 run() 抛异常——
  调用方必须仍拿到 RunResult，写盘失败降级为可见告警（同 history/证据落盘纪律）。
- heals：run.json 缺失/被手改损坏时，`mat heal list/apply` 给可行动报错，
  不甩 traceback（web 端 400 而非 500）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine.dsl import TestCase
from insta_engine.heals import collect_proposals
from insta_engine.runner import Runner


class _MinimalDriver:
    """最小设备桩：wait 步可执行、not-exists 断言可过。"""

    serial = None
    platform = "android"

    def dump(self) -> str:
        return "<hierarchy stable/>"

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def wake_and_unlock(self) -> None:
        return None

    def start_keepalive(self) -> None:
        return None

    def stop_keepalive(self) -> None:
        return None

    def _absent_node(self):
        class _Node:
            exists = False

            def wait(self, timeout: float = 0, raise_error: bool = False) -> bool:
                return False

        return _Node()

    def find(self, by: str, value: str, timeout: float):
        return self._absent_node()


def _case() -> TestCase:
    return TestCase.model_validate({
        "caseId": "persist_guard", "name": "写盘降级",
        "target": {"package": "com.insta.demo"},
        "steps": [{
            "id": "s1", "intent": "断言不存在", "action": "wait", "params": {"ms": 5},
            "assertions": [{"type": "not-exists",
                            "target": {"strategy": "res-id", "value": "com.x:id/none"}}],
        }],
    })


def test_run_returns_result_when_run_json_write_fails(tmp_path, capsys):
    """P1-1 回归：run.json 写盘失败不得毁掉一次已完成的执行。"""
    (tmp_path / "run.json").mkdir()  # 占位目录 ⇒ write_text 抛 IsADirectoryError(OSError)

    run = Runner(_MinimalDriver(), tmp_path).run(_case())

    assert run.status == "passed", "执行结果必须照常返回"
    assert "run.json 写盘失败" in capsys.readouterr().err, "写盘失败必须可见（stderr 告警）"


def test_collect_proposals_missing_run_json_is_actionable(tmp_path):
    """P1-2 回归：run.json 不存在 ⇒ 可行动 ValueError（重跑生成新产物），不是 FileNotFoundError。"""
    with pytest.raises(ValueError, match="不可读.*重跑"):
        collect_proposals(tmp_path / "ghost_run")


def test_collect_proposals_corrupt_run_json_is_actionable(tmp_path):
    """P1-2 回归：run.json 被手改坏 ⇒ 可行动 ValueError，不是裸 JSONDecodeError/traceback。"""
    (tmp_path / "run.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="已损坏.*重跑"):
        collect_proposals(tmp_path)


def test_collect_proposals_still_reads_valid_run_json(tmp_path):
    """正面用例：正常 run.json 的提案提取行为不变。"""
    (tmp_path / "run.json").write_text(json.dumps({
        "run": {"steps": [
            {"step_id": "s1", "heal": {"original": "fuzzy:textContains=登录",
                                       "match": "text=登录"}},
            {"step_id": "s2"},
        ]},
    }, ensure_ascii=False), encoding="utf-8")
    proposals = collect_proposals(tmp_path)
    assert len(proposals) == 1 and proposals[0]["step"] == "s1"
