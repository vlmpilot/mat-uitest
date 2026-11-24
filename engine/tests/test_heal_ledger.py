"""自愈跨次台账（`heal_ledger.py`）的契约测试。

它守三件事：
1. **只记真实发生过的自愈**（没有 heal 的步骤不留噪声）；
2. **`recovered` 两态都留**——"救回来过几次"与"没救回来过几次"同样重要；
3. **聚合能跨次回答"这处出现过几次、在哪些版本上"**，且重复 record 不重复计数。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from insta_engine import heal_ledger as hl


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(hl, "state_home", lambda: tmp_path)
    return tmp_path


def _run(case_id: str, *steps) -> SimpleNamespace:
    return SimpleNamespace(case_id=case_id, steps=list(steps))


def _step(step_id: str, heal: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(step_id=step_id, heal=heal)


HEAL = {"original": "res-id=com.insta.demo:id/login_btn_v2",
        "match": "text=立即登录", "via": "index-hints", "recovered": True}


def test_records_only_steps_with_heal(ledger):
    n = hl.record_run(_run("c1", _step("s1"), _step("s2", HEAL)),
                      app_package="com.insta.demo", app_version="1.0")
    assert n == 1
    rows = hl.read_rows()
    assert len(rows) == 1
    assert rows[0]["step_id"] == "s2" and rows[0]["recovered"] is True
    assert rows[0]["app_version"] == "1.0"


def test_records_missed_heal_too(ledger):
    """没救回来也要记：它回答"这处兜底失败过几次"。"""
    missed = {**HEAL, "recovered": False}
    hl.record_run(_run("c1", _step("s2", missed)))
    assert hl.read_rows()[0]["recovered"] is False


def test_summarize_groups_across_runs(ledger):
    hl.record_run(_run("c1", _step("s2", HEAL)), app_version="1.0", run_uid="run-1")
    hl.record_run(_run("c1", _step("s2", {**HEAL, "recovered": False})),
                  app_version="1.1", run_uid="run-2")
    hl.record_run(_run("c1", _step("s2", {**HEAL, "via": "local-fuzzy"})), run_uid="run-3")

    rows = hl.summarize()
    assert len(rows) == 1, "同一处（用例+步骤+原定位）应聚合成一行"
    g = rows[0]
    assert g["count"] == 3 and g["recovered"] == 2 and g["missed"] == 1
    assert g["via"] == ["index-hints", "local-fuzzy"]
    assert g["versions"] == ["1.0", "1.1"]


def test_duplicate_record_of_same_run_is_not_double_counted(ledger):
    """追加写不做去重，聚合要按 (运行时刻, 用例, 步骤) 去重。"""
    run = _run("c1", _step("s2", HEAL))
    hl.record_run(run, run_uid="run-X")
    hl.record_run(run, run_uid="run-X")  # 同一个 run 被记两次（重跑 record、脚本重入）
    assert len(hl.read_rows()) == 2
    assert hl.summarize()[0]["count"] == 1, "两次同刻记录只算一处一次"


def test_bad_lines_are_skipped(ledger):
    """台账是观测数据：一行损坏不该让整个台账不可读。"""
    path = hl.ledger_path()
    path.write_text(json.dumps({"case_id": "c1", "step_id": "s1",
                                "original": "x", "ts": "2026-11-24T10:00:00+00:00",
                                "recovered": True}) + "\n{坏行\n", encoding="utf-8")
    rows = hl.read_rows()
    assert len(rows) == 1


def test_empty_ledger_returns_empty_list(ledger):
    assert hl.read_rows() == []
    assert hl.summarize() == []
