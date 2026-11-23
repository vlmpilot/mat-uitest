"""verify 调用计量的回归：口径、零内容、静默失败、与 run_verify 的联动。

conftest 全局把 `MAT_AI_METRICS` 设为 0，需要计量的用例必须显式打开，
并把 `MAT_HOME` 指向临时目录——否则会把假数据写进用户真实状态目录。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from insta_engine import __main__ as cli
from insta_engine import verify as v
from insta_engine import verify_usage as u


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("MAT_HOME", str(tmp_path))
    monkeypatch.delenv("INSTA_HOME", raising=False)
    monkeypatch.setenv("MAT_AI_METRICS", "1")
    return tmp_path


def _verdict(name: str = "pass") -> dict:
    return {"schema": "mat.verify/1", "verdict": name, "exit_code": 0, "ok": name == "pass"}


def test_record_writes_metadata_only(home):
    """只记四项元数据；用例名/路径/截图/输入一个字节都不落盘。"""
    rich = {"schema": "mat.verify/1", "verdict": "pass", "exit_code": 0,
            "case": "登录用例", "artifacts": {"dir": "/secret/project/artifacts"},
            "notes": ["用户输入：手机号 13800000000"]}
    u.record(rich, 0)
    row = json.loads(u.usage_path().read_text(encoding="utf-8").strip())
    assert set(row) == {"ts", "schema", "verdict", "exit_code"}
    text = u.usage_path().read_text(encoding="utf-8")
    for leak in ("登录用例", "/secret/project", "13800000000", "notes", "case"):
        assert leak not in text


def test_record_disabled_by_shared_switch(home, monkeypatch):
    """与 AI 计量共用总开关：关掉就不写盘（也是全量回归不污染的保障）。"""
    monkeypatch.setenv("MAT_AI_METRICS", "0")
    u.record(_verdict(), 0)
    assert not u.usage_path().exists()


@pytest.mark.parametrize("ts,verdict,code", [
    ("2026-11-10T08:00:00+00:00", "pass", 0),
    ("2026-11-09T08:00:00+00:00", "fail", 2),
    ("2026-10-01T08:00:00+00:00", "blocked", 3),
])
def test_record_accepts_all_verdicts(home, ts, verdict, code):
    u.record(_verdict(verdict), code,
             now=datetime.fromisoformat(ts))
    row = json.loads(u.usage_path().read_text(encoding="utf-8").strip())
    assert row["verdict"] == verdict and row["exit_code"] == code


def test_summarize_window_day_verdict_and_gate(home):
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    for i, name in enumerate(("pass", "fail", "pass")):
        u.record(_verdict(name), 0 if name == "pass" else 2,
                 now=now - timedelta(days=i))
    u.record(_verdict(), 0, now=now - timedelta(days=90))  # 窗口外，只进累计
    sm = u.summarize(days=7, now=now)
    assert sm["total_all"] == 4
    assert sm["in_window"] == 3
    assert sm["by_verdict"] == {"fail": 1, "pass": 2}
    assert sum(sm["by_day"].values()) == 3
    assert sm["gate_calls"] == 100 and sm["gate_met"] is False
    assert sm["path"] == str(u.usage_path())


def test_since_overrides_days(home):
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    u.record(_verdict(), 0, now=now - timedelta(days=2))
    u.record(_verdict(), 0, now=now - timedelta(days=20))
    assert u.summarize(days=7, now=now)["in_window"] == 1
    assert u.summarize(days=7, since="2026-10-01", now=now)["in_window"] == 2
    # since 是精确起算日（UTC 00:00），不是「再往前放宽 N 天」
    assert u.summarize(days=7, since="2026-11-09", now=now)["in_window"] == 0
    assert u.summarize(days=7, since="2026-11-08", now=now)["in_window"] == 1


def test_gate_met_boundary(home, monkeypatch):
    monkeypatch.setattr(u, "GATE_CALLS", 3)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    for _ in range(3):
        u.record(_verdict(), 0, now=now)
    sm = u.summarize(days=28, now=now)
    assert sm["in_window"] == 3 and sm["gate_met"] is True


def test_bad_lines_and_rotation_are_counted(home):
    path = u.usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    path.with_suffix(path.suffix + ".1").write_text(
        json.dumps({"ts": now.isoformat(timespec="seconds"),
                    "schema": "mat.verify/1", "verdict": "pass", "exit_code": 0}) + "\n",
        encoding="utf-8")
    path.write_text(
        "not-json\n" + json.dumps({"ts": "bad-date"}) + "\n"
        + json.dumps({"ts": now.isoformat(timespec="seconds"),
                      "schema": "mat.verify/1", "verdict": "fail", "exit_code": 2}) + "\n",
        encoding="utf-8")
    sm = u.summarize(days=28, now=now)
    assert sm["total_all"] == 2 and sm["in_window"] == 2
    assert sm["by_verdict"] == {"fail": 1, "pass": 1}


def test_record_never_raises_on_unwritable_path(home, monkeypatch, tmp_path):
    """计量坏掉不能把 verify 结论搞挂：目录当文件写也只静默返回。"""
    monkeypatch.setattr(u, "usage_path", lambda: tmp_path / "metrics")
    (tmp_path / "metrics").mkdir()
    u.record(_verdict(), 0)


def test_run_verify_records_once(home, monkeypatch, tmp_path):
    """三条调用路径共用的薄壳：一次 run_verify 恰好一行。"""
    monkeypatch.setattr(v, "_run_verify", lambda *a, **k: (0, _verdict()))
    monkeypatch.setattr(v, "_persist_verdict", lambda _verdict: None)
    code, _ = v.run_verify(tmp_path, tmp_path / "case.json")
    assert code == 0
    rows = u.usage_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["verdict"] == "pass"


def test_render_states_local_only_boundary(home):
    text = u.render(u.summarize())
    assert "不上传" in text
    assert "≥3 个" in text
    assert "本机" in text
    assert "明细" in text


def test_cmd_verify_stats_json(home, capsys):
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    u.record(_verdict("fail"), 2, now=now)
    rc = cli.cmd_verify_stats(SimpleNamespace(days=28, since="2026-10-31", json=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["in_window"] == 1 and out["by_verdict"] == {"fail": 1}
    assert out["gate_calls"] == 100


def test_cmd_verify_stats_rejects_bad_since(home, capsys):
    rc = cli.cmd_verify_stats(SimpleNamespace(days=28, since="上周", json=False))
    assert rc == 1
    assert "YYYY-MM-DD" in capsys.readouterr().err
