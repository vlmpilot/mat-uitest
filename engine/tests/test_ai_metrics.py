"""AI 计量与输出预算的契约测试。

守两条纪律：
1. **计量只记元数据**——`nl` / `app_info` / 模型输出一个字节都不进 `ai.jsonl`，
   否则会把「只记次数、不存内容」的承诺从客户端这边破掉；
2. **输出预算按任务算**——convert 的输出是整条用例，默认 4096 会在 ~15 步后静默截断。
"""

from __future__ import annotations

import json

import pytest

from insta_engine import ai_client, ai_metrics


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """把状态目录指到临时目录，避免污染真实 ~/.mat。"""
    monkeypatch.setenv("MAT_HOME", str(tmp_path))
    monkeypatch.delenv("INSTA_HOME", raising=False)
    monkeypatch.setenv("MAT_AI_METRICS", "1")   # conftest 默认关，这里显式开
    monkeypatch.delenv("MAT_GEN_TRACE", raising=False)
    return tmp_path


def _rows() -> list[dict]:
    path = ai_metrics.metrics_path()
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_record_keeps_metadata_and_drops_content(home):
    ai_metrics.record(task="gen", model="qwen-plus", ok=True, latency_ms=1234,
                      mode="local", prompt_tokens=9000, completion_tokens=1200,
                      prompt_chars=42000, completion_chars=3000, max_tokens=4096,
                      truncated=False)
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["task"] == "gen" and row["prompt_tokens"] == 9000
    assert row["completion_tokens"] == 1200 and row["latency_ms"] == 1234
    # 关键：不许出现任何内容字段
    for forbidden in ("variables", "raw_output", "nl", "app_info", "content", "prompt"):
        assert forbidden not in row


def test_metrics_can_be_disabled(home, monkeypatch):
    monkeypatch.setenv("MAT_AI_METRICS", "0")
    ai_metrics.record(task="gen", model="m")
    assert _rows() == []


def test_trace_is_off_by_default(home):
    """留痕默认关——开了就落 PII，必须显式同意。"""
    assert ai_metrics.trace(task="gen", model="m", variables={"nl": "登录"},
                            raw_output="{}") is None
    assert not (ai_metrics.metrics_dir() / ai_metrics.TRACE_DIR).exists()


def test_trace_writes_local_file_with_meta_when_enabled(home, monkeypatch):
    monkeypatch.setenv("MAT_GEN_TRACE", "1")
    path = ai_metrics.trace(task="gen", model="m", variables={"nl": "登录"},
                            raw_output='{"cases":[]}')
    assert path is not None and path.exists()
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["variables"]["nl"] == "登录" and body["raw_output"] == '{"cases":[]}'
    meta = json.loads((path.parent / "_meta.json").read_text(encoding="utf-8"))
    assert meta["contains_user_data"] is True, "必须标注含用户数据，防止被当公开数据拷走"


def test_rotation_keeps_one_previous_file(home, monkeypatch):
    monkeypatch.setenv("MAT_METRICS_MAX_MB", "0")   # 任何写入都触发轮转
    ai_metrics.record(task="gen", model="m")
    first = ai_metrics.metrics_path()
    assert first.exists()
    ai_metrics.record(task="gen", model="m")
    assert first.with_suffix(first.suffix + ".1").exists()
    assert len(_rows()) == 1, "轮转后当前文件只保留新的一行"


def test_summarize_groups_by_task(home):
    for i in range(3):
        ai_metrics.record(task="gen", model="m", latency_ms=1000 + i * 1000,
                          prompt_tokens=100, completion_tokens=10)
    ai_metrics.record(task="gen", model="m", ok=False, latency_ms=500, error="429")
    ai_metrics.record(task="convert", model="m", latency_ms=200, prompt_tokens=50)

    s = ai_metrics.summarize(hours=24)
    assert s["total_calls"] == 5
    gen = s["by_task"]["gen"]
    assert gen["calls"] == 4 and gen["failed"] == 1
    assert gen["prompt_tokens"] == 300
    assert gen["p50_ms"] == 2000 and gen["p95_ms"] >= gen["p50_ms"]
    assert s["by_task"]["convert"]["calls"] == 1


def test_output_budget_only_applies_to_convert():
    assert ai_client._output_budget("gen", {"nl": "x"}) == (None, "")
    assert ai_client._output_budget("polish", {}) == (None, "")


def test_output_budget_scales_with_steps_and_caps():
    """convert 按步数给预算：少步数保底 4096，超长用例封顶并**出声**。"""
    small = ai_client._output_budget("convert", {"ir": {"steps": [{}] * 5}})
    assert small == (4096, ""), "5 步用不到 4096，保底不缩水"

    mid = ai_client._output_budget("convert", {"ir": {"steps": [{}] * 20}})
    assert mid[0] == 800 + 220 * 20 and mid[1] == ""

    huge_budget, warn = ai_client._output_budget("convert", {"ir": {"steps": [{}] * 60}})
    assert huge_budget == 8192 and "分段转换" in warn, "超长用例要提示分段，而不是静默截断"


class _FakeResp:
    status_code = 200
    headers: dict = {}

    def __init__(self, content: str = '{"cases": []}'):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"model": "fake-model",
                "choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 1234, "completion_tokens": 56}}


def test_max_tokens_argument_reaches_payload_and_metrics(home, monkeypatch):
    monkeypatch.delenv("GEN_MAX_TOKENS", raising=False)
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return _FakeResp()

    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    client = ai_client.GenClient("http://example.invalid", "k", "m")
    out = client.complete_raw_text([{"role": "user", "content": "hi"}], "转换",
                                   task="convert", max_tokens=8192)
    assert out == '{"cases": []}'
    assert captured["max_tokens"] == 8192, "按步数算的预算必须真的进请求体"

    rows = _rows()
    assert rows and rows[0]["task"] == "convert"
    assert rows[0]["prompt_tokens"] == 1234 and rows[0]["completion_tokens"] == 56
    assert rows[0]["max_tokens"] == 8192


def test_env_max_tokens_wins_over_argument(home, monkeypatch):
    monkeypatch.setenv("GEN_MAX_TOKENS", "2048")
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return _FakeResp()

    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    client = ai_client.GenClient("http://example.invalid", "k", "m")
    client.complete_raw_text([{"role": "user", "content": "hi"}], "转换",
                             task="convert", max_tokens=8192)
    assert captured["max_tokens"] == 2048, "显式环境变量是最高优先级（排障口子）"


def test_calibration_round_accepts_split_cases_output(home, monkeypatch):
    """校准轮必须和首轮用同一个宽松校验器（`_validate_single`）。

    实测背景（2026-10-21）：gen prompt 现在默认产出 `{"cases":[...]}`，而校准轮用的是
    严格的 `TestCase.model_validate` ⇒ 第 1 轮就抛 `caseId/steps Field required`，
    整条校准回路崩掉——表现为"生成能过、一加 --calibrate 必死"。
    """
    from types import SimpleNamespace

    from insta_engine import ai_client

    payload = {"cases": [{
        "caseId": "case_000001", "name": "外卖页加载", "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "launch"}],
    }]}

    calls: list[str] = []

    def fake_complete(client, task, variables, context, **kw):
        calls.append(context)
        # 真实实现会在内部跑 validator；桩也要照做，否则拿到的还是裸 dict
        validator = kw.get("validator")
        return validator(payload) if validator else payload

    monkeypatch.setattr(ai_client, "complete_task_raw", fake_complete)

    def fake_executor(case):
        return SimpleNamespace(status="failed", steps=[
            SimpleNamespace(step_id="s1", intent="", category="locate", error="未命中", status="failed"),
        ])

    client = ai_client.GenClient("http://example.invalid", "k", "m")
    case, history = client.generate_calibrated("进入外卖页", {"package": "com.x"},
                                              executor=fake_executor, max_rounds=2)
    assert case.case_id == "case_000001"
    assert len(calls) == 3, "首轮 + 2 轮校准都应调用"
    assert len(history) == 2 and history[0]["status"] == "failed"


# -- 生成成功率口径分家（2026-10-25） ------------------------------

def test_task_scope_rows_and_first_pass_rate(home):
    """scope=task 行记逻辑任务结局；首穿率=0 轮内容修复即合法的比例。

    口径分家：单次生成成功率（本处）≠ 端到端跑通率，summarize 分开统计、
    render 分行输出，永不混引。
    """
    ai_metrics.record(task="gen", ok=True, scope="task", rounds=1)
    ai_metrics.record(task="gen", ok=True, scope="task", rounds=2)   # 修一轮后成功
    ai_metrics.record(task="gen", ok=False, scope="task", rounds=3)
    ai_metrics.record(task="gen", ok=True)                            # 传输层行,不进首穿口径

    s = ai_metrics.summarize(hours=1)
    fp = s["task_outcomes"]["gen"]
    assert fp["tasks"] == 3, "传输层 call 行不得混入逻辑任务口径"
    assert fp["ok_rate"] == round(2 / 3, 3)
    assert fp["first_pass_rate"] == round(1 / 3, 3)
    assert s["by_task"]["gen"]["calls"] == 1, "call 行照常按传输层统计"
    text = ai_metrics.render(s)
    assert "首穿 33%" in text


class _ScriptedClient:
    """complete_raw_text 按脚本依次返回:第 1 个用例首穿;第 2 个先坏后好。"""

    mode = "local"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete_raw_text(self, messages, context, task="", max_tokens=None):
        self.calls += 1
        return self.script.pop(0)


def test_complete_records_task_outcome_rounds(home):
    """complete() 的内容修复循环按逻辑任务记账:首穿 rounds=1,修复后 rounds=2。"""
    from insta_engine.ai_client import complete_task_raw

    good = json.dumps({"ok": 1})
    stub = _ScriptedClient([good, "不是JSON", good])

    out = complete_task_raw(stub, "convert", {}, context="首穿:")
    assert out == {"ok": 1}
    out2 = complete_task_raw(stub, "convert", {}, context="修复:")
    assert out2 == {"ok": 1}

    s = ai_metrics.summarize(hours=1)
    fp = s["task_outcomes"]["convert"]
    assert fp["tasks"] == 2 and fp["first_pass_rate"] == 0.5, fp


def test_total_calls_excludes_task_rows(home):
    """total_calls 只数传输层调用:逻辑任务行不能把它抬上去。

    病灶(2026-10-25 实测):某台机器近 24 小时 `mat ai-stats` 首行报 13 次调用,
    其中 8 次是真调用、5 次是 scope=task 的逻辑任务行 ⇒ 首行数字与下面按任务
    列出的 calls 加总(8)对不上,读的人以为是漏统计。
    """
    for _ in range(3):
        ai_metrics.record(task="gen", model="m", latency_ms=100, scope="call")
    ai_metrics.record(task="gen", model="m", latency_ms=900, scope="task", rounds=1)
    ai_metrics.record(task="gen", model="m", latency_ms=900, scope="task", rounds=2)

    s = ai_metrics.summarize(hours=24)
    assert s["total_calls"] == 3, "task 行不得计入调用次数"
    assert s["by_task"]["gen"]["calls"] == 3
    assert s["task_outcomes"]["gen"]["tasks"] == 2, "task 行仍要在首穿口径里"
    # 渲染首行与明细加总必须一致
    assert s["total_calls"] == sum(t["calls"] for t in s["by_task"].values())
