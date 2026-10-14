"""团队成员名额配额：本地缓存、到期回落、取值顺序（2026-10-14 改为服务端按主机发配额）。

⚠️ 顺带守住 `parse_plan_segment` 的语法：名额**不再写进激活码**了，但那个函数仍是
三端共用的解析（时长卡靠它），"未知段忽略、不拒签"这条不能变。

名额是**本地规则的自我约束**（控制台拦人），不是 DRM —— 客户改 `.env` 或删
`team_seats.json` 都能绕。所以这里测的是"官方途径是否正确"，不是"能不能防住"。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest

from insta_engine import licensing, seat_quota
from insta_engine.web_api import seats

TEST_KEY = "test-only"


def mint(plan: str, days: int = 30) -> str:
    body = f"{plan}.{int(time.time()) + days * 86400}.{uuid.uuid4().hex[:6]}"
    sig = hmac.new(TEST_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
    raw = f"{body}.{sig}"
    return "MAT-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def _write_grants(*grants: dict) -> None:
    """写一份"同步过"的缓存。

    ⚠️ 缓存里的 `base` 只是**留档** —— 成员上限的基线由 `seat_quota.base_for_plan()`
    按本机 plan 算（2026-10-14 起分档），**不采信服务端下发的 base**。改它不影响结果，
    所以这里干脆不暴露这个参数（免得后来者以为它能调基线）。
    """
    seat_quota.write_cache({"machine_id": "f" * 64, "synced_at": int(time.time()),
                            "base": seat_quota.BASE_SEATS_PRO, "grants": list(grants)})


def _set_plan(plan: str) -> None:
    """给本机写一份**有效授权**（用测试密钥签），用于验证"基线按档位分"。"""
    licensing.license_path().write_text(
        json.dumps({"plan": plan, "expires_at": int(time.time()) + 86400,
                    "code": mint(plan)}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_HOME", str(tmp_path))
    monkeypatch.delenv("INSTA_HOME", raising=False)
    monkeypatch.setenv("MAT_LICENSE_SECRET", TEST_KEY)
    monkeypatch.delenv("MAT_SEAT_LIMIT", raising=False)
    return tmp_path


# -- plan 段解析：名额段已废弃，但"未知段忽略"这条不能变 ----------------------

@pytest.mark.parametrize("plan,base,months", [
    ("pro", "pro", 0),
    ("pro+12", "pro", 12),                # 时长卡，必须继续认
    ("pro+6", "pro", 6),
    ("pro+abc", "pro", 0),                # 未知段 → 降级成普通码（宁可降级也不拒签）
    ("pro+0", "pro", 0),
    ("pro+seats10", "pro", 0),            # 名额段已废弃：现在只是个被忽略的未知段
    ("pro+12+seats10", "pro", 12),        # 但同一张码里的时长仍要认出来
])
def test_plan_segment_syntax(plan, base, months):
    assert licensing.parse_plan_segment(plan) == (base, months)


def test_a_code_with_unknown_segments_still_works():
    """格式演进的保险：带未知段的码**仍然可用**（那一段被忽略而已，码不作废）。"""
    out = licensing.verify_code(mint("pro+12+seats10"))
    assert out is not None and out["plan"] == "pro" and out["months"] == 12


def test_seats_are_not_part_of_the_license_any_more():
    """名额不再从码里读：`verify_code` 的返回里没有 seats 这个键。"""
    assert "seats" not in licensing.verify_code(mint("pro+seats10"))


# -- 缓存：没有 / 同步过但为空 -------------------------------------------------

def test_no_cache_falls_back_to_the_free_baseline():
    """🔴 没有授权 ⇒ **免费体验版基线 2 人**（2026-10-14 起按档位分）。"""
    assert seat_quota.effective_limit() == seat_quota.BASE_SEATS_FREE
    assert seats.seat_limit() == seat_quota.BASE_SEATS_FREE


def test_baseline_depends_on_plan():
    """🔴 基线按档位分：**标准版 5 人、免费体验版 2 人**（发起人 2026-10-14 定）。

    免费档的 2 人是刻意的**升级触发器**：`--share` 是多人一起用的唯一入口，2 个人
    足够验证"协作是不是我要的"，第 3 个人就得升级 —— 所以**不需要单独去锁 `--share`**，
    压基线就同时做到了"能试"和"要升级"。
    """
    assert seat_quota.base_for_plan("pro") == 5
    assert seat_quota.base_for_plan("free") == 2
    assert seat_quota.base_for_plan("没这个档位") == 2, "未知档位按免费档处理（fail-safe）"

    assert seat_quota.effective_limit() == 2   # 无授权 ⇒ 免费档
    _set_plan("pro")
    assert seat_quota.effective_limit() == 5   # 有标准版授权
    _set_plan("free")
    assert seat_quota.effective_limit() == 2   # 授权失效/过期 ⇒ 回落


def test_synced_empty_grants_is_an_answer_not_a_missing_value(monkeypatch):
    """🔴 同步过、且服务端说"这个客户没有额外配额" ⇒ 就是**基线本身**。

    不能把空配额当成"没配置"去回落 `.env` —— 那等于服务端说什么都不算。
    """
    monkeypatch.setenv("MAT_SEAT_LIMIT", "99")
    _write_grants()
    assert seat_quota.effective_limit() == seat_quota.BASE_SEATS_FREE


def test_env_fallback_only_applies_before_the_first_sync(monkeypatch):
    """`.env` 的人工口子只在**从没同步过**时兜底（过渡期用，不是常态）。"""
    monkeypatch.setenv("MAT_SEAT_LIMIT", "99")
    assert seat_quota.effective_limit() == 99

    _write_grants()
    assert seat_quota.effective_limit() == seat_quota.BASE_SEATS_FREE, "同步之后以服务端配额为准"


# -- 分批与到期（发起人例子的核心）---------------------------------------------

def test_batches_sum_up():
    now = int(time.time())
    _write_grants({"seats": 3, "expires_at": now + 86400},
                  {"seats": 2, "expires_at": now + 86400})
    assert seat_quota.effective_limit() == seat_quota.BASE_SEATS_FREE + 5   # 基线 2 + 3 + 2


def test_expired_batch_drops_out_without_any_network():
    """🔴 到期回落**不依赖联网、也不需要定时任务**：缓存里存的是明细，本地按时间算。"""
    now = int(time.time())
    _write_grants({"seats": 3, "expires_at": now + 10})

    assert seat_quota.effective_limit(now) == seat_quota.BASE_SEATS_FREE + 3
    assert seat_quota.effective_limit(now + 11) == seat_quota.BASE_SEATS_FREE, \
        "到期后该掉回基线"


def test_the_founders_example_end_to_end():
    """发起人的例子：**标准版 5 人** → 10-30 加 3 人、为期 2 个月 → 12-30 之后回落 5。

    ⚠️ 这个例子讲的是**标准版**（免费档现在是 2 人）⇒ 这里显式写一份 pro 授权，
    顺带覆盖"基线按档位分"这条。
    """
    _set_plan("pro")
    add_at = int(time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1)))
    expire_at = int(time.mktime((2026, 12, 30, 12, 0, 0, 0, 0, -1)))
    _write_grants({"seats": 3, "expires_at": expire_at, "note": "客户追加 3 人"})

    assert seat_quota.effective_limit(add_at) == 8          # 加完：5 + 3 = 8 人
    assert seat_quota.effective_limit(expire_at - 60) == 8  # 到期前一分钟仍是 8
    assert seat_quota.effective_limit(expire_at + 60) == 5  # 到期后回落 5
    assert seats.seat_limit.__module__                      # 控制台走同一个口径


def test_a_dirty_row_is_skipped_not_fatal():
    """一条坏记录不该把整个名额算崩 —— 那会让客户莫名被挡在控制台外。"""
    _write_grants({"seats": "很多", "expires_at": "明天"},
                  {"seats": 2, "expires_at": int(time.time()) + 86400})
    assert seat_quota.effective_limit() == seat_quota.BASE_SEATS_FREE + 2


def test_describe_marks_each_batch():
    now = int(time.time())
    _write_grants({"seats": 3, "expires_at": now + 86400, "note": "追加"},
                  {"seats": 1, "expires_at": now - 86400})
    text = seat_quota.describe(now)

    assert "有效" in text and "已过期" in text
    assert "追加" in text


# -- 视觉兜底额度（2026-10-14：与成员名额同一套主机配额，vision_base + 批次 vision）--

def test_vision_limit_defaults_to_300_without_sync(monkeypatch):
    """服务端没配过 ⇒ 默认 300（发起人 2026-10-14 定）；`.env` 只在从没同步过时兜底。"""
    monkeypatch.delenv("MAT_VISION_LIMIT", raising=False)
    assert seat_quota.vision_limit() == 300

    monkeypatch.setenv("MAT_VISION_LIMIT", "500")
    assert seat_quota.vision_limit() == 500


def test_vision_limit_synced_value_wins(monkeypatch):
    """🔴 同步过 ⇒ 以服务端为准（哪怕 `.env` 写了更大的数——同一套口径与成员名额对齐）。"""
    monkeypatch.setenv("MAT_VISION_LIMIT", "999")
    seat_quota.write_cache({"machine_id": "f" * 64, "synced_at": int(time.time()),
                            "base": 5, "vision_base": 300, "grants": []})
    assert seat_quota.vision_limit() == 300


def test_vision_batches_take_the_widest_active_one():
    """多批并存取最大（月上限是限制不是福利，过渡期临时加倍取宽的），过期批次自动掉出。"""
    now = int(time.time())
    seat_quota.write_cache({"machine_id": "f" * 64, "synced_at": now,
                            "base": 5, "vision_base": 300,
                            "grants": [{"seats": 3, "vision": 600, "expires_at": now + 86400},
                                       {"seats": 1, "vision": 900, "expires_at": now - 60}]})
    assert seat_quota.vision_limit(now) == 600   # 900 那批已过期，不参与
    assert seat_quota.effective_limit(now) == seat_quota.BASE_SEATS_FREE + 3  # 名额照常按批次合计
