"""日常 flaky 统计（`flaky.py`）的契约测试（评审 #16）。

守三类口径决定：
1. `cancelled` 既非稳也非不稳 ⇒ 不进通过率；
2. 跑得少的（< min_runs）不判 flaky —— 2 次跑成 1/1 是噪声，不是"不稳"；
3. 测试桩默认排除（单测曾往同一份历史写 `c1`/`n`，会把统计变成"测试跑了几遍"）。
"""

from __future__ import annotations

from insta_engine import flaky as fl


def _rows():
    ts = "2026-11-24T10:00:00+00:00"
    rows = []
    # A：稳定通过 4 次
    rows += [{"ts": ts, "case_id": "A", "case_name": "搜索", "status": "passed"}] * 4
    # B：3 次里 1 过 2 不过 ⇒ flaky
    rows += [{"ts": ts, "case_id": "B", "case_name": "登录", "status": "passed"},
             {"ts": ts, "case_id": "B", "case_name": "登录", "status": "failed"},
             {"ts": ts, "case_id": "B", "case_name": "登录", "status": "failed"}]
    # C：全失败（稳定失败，不算 flaky）
    rows += [{"ts": ts, "case_id": "C", "case_name": "崩溃", "status": "failed"}] * 3
    # D：只有 2 次 ⇒ 默认不统计
    rows += [{"ts": ts, "case_id": "D", "case_name": "少跑", "status": "passed"},
             {"ts": ts, "case_id": "D", "case_name": "少跑", "status": "failed"}]
    # E：cancelled 不算
    rows += [{"ts": ts, "case_id": "E", "case_name": "取消", "status": "cancelled"}] * 5
    return rows


def test_flaky_detection_and_ordering():
    s = fl.summarize(_rows(), min_runs=3, days=0)
    by_id = {it["case_id"]: it for it in s["items"]}
    assert by_id["A"]["flaky"] is False and by_id["A"]["pass_rate"] == 1.0
    assert by_id["B"]["flaky"] is True and by_id["B"]["pass_rate"] == 0.3333
    assert by_id["C"]["flaky"] is False, "全失败是稳定失败，不是 flaky"
    assert "D" not in by_id, "跑得少的不判 flaky"
    assert "E" not in by_id, "cancelled 不进通过率"
    assert s["flaky_cases"] == 1
    assert s["items"][0]["case_id"] == "B", "flaky 排最前"


def test_stub_rows_are_excluded_by_default(tmp_path):
    rows = _rows() + [{"ts": "2026-11-24T10:00:00+00:00", "case_id": "c1",
                       "case_name": "n", "status": "passed"}] * 10
    s = fl.summarize(rows, min_runs=3, days=0)
    assert s["excluded_stub_rows"] == 10
    assert all(it["case_id"] != "c1" for it in s["items"])

    s_all = fl.summarize(rows, min_runs=3, days=0, include_stubs=True)
    assert any(it["case_id"] == "c1" for it in s_all["items"])


def test_exclude_pattern_removes_known_noise():
    """历史里的旧噪声识别不了（名字像真用例），留一个显式开关。"""
    rows = _rows() + [{"ts": "2026-11-24T10:00:00+00:00", "case_id": "selfheal_noop",
                       "case_name": "自愈", "status": "passed"},
                      {"ts": "2026-11-24T10:00:00+00:00", "case_id": "selfheal_noop",
                       "case_name": "自愈", "status": "failed"}]
    s = fl.summarize(rows, min_runs=2, days=0, exclude="^selfheal_")
    assert all(it["case_id"] != "selfheal_noop" for it in s["items"])


def test_days_window_filters_old_rows():
    rows = [{"ts": "2020-01-01T00:00:00+00:00", "case_id": "OLD",
             "case_name": "旧", "status": "passed"}] * 5
    assert fl.summarize(rows, min_runs=1, days=30)["items"] == []
    assert fl.summarize(rows, min_runs=1, days=0)["items"], "days=0 表示不限"


def test_load_rows_skips_bad_lines(tmp_path):
    p = tmp_path / "history.jsonl"
    p.write_text('{"case_id":"A","status":"passed","ts":"2026-11-24T10:00:00+00:00"}\n'
                 '{坏行\n\n', encoding="utf-8")
    assert len(fl.load_rows(p)) == 1


def test_render_mentions_excluded_and_empty_states():
    s = fl.summarize([], min_runs=3, days=30)
    assert "没有可统计的用例" in fl.render(s)
    s2 = fl.summarize(_rows(), min_runs=3, days=0)
    text = fl.render(s2)
    assert "flaky" in text and "已排除" in text
