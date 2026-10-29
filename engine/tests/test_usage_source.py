"""用量按来源拆分（2026-10-28）：官方代理 vs 自带 Key。

拆分的**唯一目的**是展示，不参与拦截：
- 官方代理：平台出 token 钱 ⇒ 服务端 429 是权威；
- 自带 Key：用户自己结算 ⇒ 不该限额，也不该被算成"官方额度已用"。

所以这里断言的不是"BYOK 也扣额度"，而是"BYOK 只记分项、不污染官方用量"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from insta_engine import promptstore
from insta_engine.quota import QuotaExceeded, UsageStore


@pytest.fixture(autouse=True)
def _free_plan(monkeypatch):
    """固定 free 档（视觉上限 20），避免读到开发机上的真实 license。"""
    monkeypatch.setattr(UsageStore, "plan", lambda self: "free")


def _store(tmp_path, source: str | None = None) -> UsageStore:
    return UsageStore(tmp_path / "usage.json", source=source)


def test_byok_counts_only_in_byok_bucket(tmp_path):
    store = _store(tmp_path, source="byok")
    store.consume(1)
    store.consume(2)

    snapshot = store.snapshot()
    assert snapshot["gen_count"] == 3
    assert snapshot["gen_byok"] == 3
    assert snapshot["gen_official"] == 0


def test_official_usage_does_not_touch_byok_bucket(tmp_path):
    store = _store(tmp_path, source="official")
    store.consume(2)

    snapshot = store.snapshot()
    assert snapshot["gen_count"] == 2
    assert snapshot["gen_byok"] == 0
    assert snapshot["gen_official"] == 2


def test_mixed_usage_splits_correctly(tmp_path):
    _store(tmp_path, source="official").consume(2)
    store = _store(tmp_path, source="byok")
    store.consume(3)

    snapshot = store.snapshot()
    assert snapshot["gen_count"] == 5
    assert snapshot["gen_byok"] == 3
    assert snapshot["gen_official"] == 2  # 差值即官方用量


def test_old_usage_file_without_source_fields_is_compatible(tmp_path):
    """升级前的 usage.json 没有 gen_byok —— 不能报错，也不能把总数丢掉。"""
    month = datetime.now(UTC).strftime("%Y-%m")
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"month": month, "gen_count": 7, "vision_count": 2}),
                    encoding="utf-8")

    snapshot = UsageStore(path).snapshot()
    assert snapshot["gen_count"] == 7
    assert snapshot["vision_count"] == 2
    assert snapshot["gen_byok"] == 0  # 历史无法追溯，按 0 起算
    assert snapshot["gen_official"] == 7


def test_vision_byok_is_counted_but_not_enforced(tmp_path):
    """自带 Key 的视觉调用：free 档上限 20 也不该拦——成本不在平台。"""
    store = _store(tmp_path, source="byok")
    for _ in range(25):
        store.check_vision(enforce=False)

    snapshot = store.snapshot()
    assert snapshot["vision_count"] == 25
    assert snapshot["vision_byok"] == 25
    assert snapshot["vision_official"] == 0


def test_vision_official_still_enforced(tmp_path):
    """官方代理的视觉调用保留原闸：超出 free 的 20 次即抛，且拦截后不再计数。"""
    store = _store(tmp_path, source="official")
    for _ in range(20):
        store.check_vision(enforce=True)
    with pytest.raises(QuotaExceeded):
        store.check_vision(enforce=True)

    snapshot = store.snapshot()
    assert snapshot["vision_count"] == 20
    assert snapshot["vision_byok"] == 0
    assert snapshot["vision_official"] == 20


def test_auto_source_follows_prompt_mode(tmp_path, monkeypatch):
    """不显式传 source 时，按当前接入方式判定（与请求走哪条路径同源）。"""
    monkeypatch.setattr(promptstore, "is_server", lambda: False)
    UsageStore(tmp_path / "usage.json").consume(1)
    assert UsageStore(tmp_path / "usage.json").snapshot()["gen_byok"] == 1

    monkeypatch.setattr(promptstore, "is_server", lambda: True)
    UsageStore(tmp_path / "usage.json").consume(1)
    snapshot = UsageStore(tmp_path / "usage.json").snapshot()
    assert snapshot["gen_count"] == 2
    assert snapshot["gen_byok"] == 1
    assert snapshot["gen_official"] == 1
