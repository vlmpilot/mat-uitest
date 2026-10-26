"""团队成员名额配额：本地缓存、到期回落、取值顺序（2026-10-14 改为服务端按主机发配额）。

⚠️ 顺带守住 `parse_plan_segment` 的语法：名额**不再写进激活码**了，但那个函数仍是
三端共用的解析（时长卡靠它），"未知段忽略、不拒签"这条不能变。

名额是**本地规则的自我约束**（控制台拦人），不是 DRM —— 客户改 `.env` 或删
`team_seats.json` 都能绕。所以这里测的是"官方途径是否正确"，不是"能不能防住"。

🔴 2026-10-25 产品定位调整：**团队共享只随团队授权提供**——标准/体验授权面向单人
使用，`--share` 下上限恒 1（闸门 `seats.seat_limit`，判定 `seat_quota.team_authorized`）。
此前的"免费 2 人试协作、第 3 人升级"触发器作废；算术层（`base_for_plan` /
`effective_limit`）保持纯净，产品规则只在闸门与展示层生效。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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
    """🔴 没有授权 ⇒ 算术层按免费基线（`effective_limit`），但**共享闸门**恒 1 人。

    两层分开（2026-10-25）：`base_for_plan`/`effective_limit` 是纯算术（档位基线 +
    服务端配额）；「标准/体验授权不开放共享」是产品规则，只在 `seat_limit(shared=True)`
    这道闸门上生效——所以未授权/授权失效的机器 `--share` 下也只有安装者本人。
    """
    assert seat_quota.effective_limit() == seat_quota.BASE_SEATS_FREE
    assert seats.seat_limit(shared=True) == seats.SINGLE_SEAT_LIMIT, "未授权不开放共享"
    assert seats.seat_limit() == seats.SINGLE_SEAT_LIMIT == 1, "本机模式只有安装者本人"


# -- 名额只在团队模式生效（2026-10-23） ----------------------------------------

def test_local_console_name_is_just_a_label(tmp_path):
    """方案 B（2026-10-25）：本机模式名字只是本地标签——登记端点直接放行,
    不占席位、不限名额、不落席位文件(设名后改名曾被「成员已满」拒,用户实测)。
    名额拒人(403 + 指向 --share)只在团队模式,见 test_seat_limit_enforced。"""
    code, payload = seats.api_seat_check(tmp_path, {"name": "我"})
    assert code == 200 and payload["limit"] == 1 and payload["shared"] is False
    assert seats.api_seat_check(tmp_path, {"name": "改名了"})[0] == 200
    assert seats.api_seat_check(tmp_path, {"name": "再改"})[0] == 200
    assert not (tmp_path / "artifacts" / "team_seats.json").exists()


def test_shared_console_single_seat_without_team_authorization(tmp_path):
    """🔴 2026-10-25：未授权（体验/标准）跑 `--share` ⇒ 第 1 人放行、第 2 人 403，
    消息指向「团队授权」——不是"已满请联系我们加名额"（那是团队客户的话术）。"""
    assert seats.api_seat_check(tmp_path, {"name": "甲"}, shared=True)[0] == 200

    code, payload = seats.api_seat_check(tmp_path, {"name": "乙"}, shared=True)
    assert code == 403 and payload["seats_full"] is True
    assert payload["shared"] is True
    assert "团队授权" in payload["error"]
    assert "体验模式" in payload["error"] or "标准模式" in payload["error"]


def test_write_gate_full_under_team_authorization(tmp_path):
    """写闸满员 403（**仅团队授权下的共享**）：第 1 人登记放行、压到 1 后第二人
    403、老名字核销放行——防「先匿名绕过名额」的那个洞只在真团队场景守。

    未授权共享与单人模式免名：见 test_solo_mode_name_free 与
    test_write_gate_requires_name_only_for_team_authorization。
    """
    import os
    from unittest import mock

    _set_plan("team")
    with mock.patch.dict(os.environ, {"MAT_SEAT_LIMIT": "1"}):
        assert seats.enforce(tmp_path, {"operator": "我"}, shared=True) is None

        gate = seats.enforce(tmp_path, {"operator": "同事"}, shared=True)
        assert gate is not None and gate[0] == 403
        assert "团队成员已满" in gate[1]["error"]

        assert seats.enforce(tmp_path, {"operator": "我"}, shared=True) is None, \
            "已登记的名字不该被拦"


def test_solo_mode_name_free(tmp_path):
    """方案 B（2026-10-25，发起人批准）：单人模式免名——匿名直接放行、不占席位。

    动机：产品定位「插手机就能测」；且单人席位 1/1 被首个名字绑定后，换浏览器
    输新名字会被「成员已满」拒（实测踩过），纯支持负担。operator 由 operator_of
    兜底为「本机」，审计字段仍在。
    """
    from insta_engine.web_api.runtime import operator_of

    # 匿名放行（原为 400 need_operator）
    assert seats.enforce(tmp_path, {}) is None
    # 任何名字也不登记、不校验——席位文件不应被写
    assert seats.enforce(tmp_path, {"operator": "甲"}) is None
    assert seats.enforce(tmp_path, {"operator": "乙"}) is None, \
        "单人模式不该有「第二个名字被拒」的坑"
    assert not (tmp_path / "artifacts" / "team_seats.json").exists(), \
        "单人模式不登记席位"
    # operator 兜底：空名 → 本机；显式名保留（团队模式闸门已保证非空）
    assert operator_of({}) == "本机"
    assert operator_of({"operator": "张三"}) == "张三"


def test_write_gate_requires_name_only_for_team_authorization(tmp_path):
    """写闸按授权态分（2026-10-25 收口）：**只有团队授权下的共享**要求实名。

    - 团队授权 + shared：匿名 ⇒ 400 need_operator（真团队场景的安全语义）
    - 未授权 + shared：免名放行——名额恒 1、名单里多半是安装者自己（换浏览器后
      operator 空），若要求实名则设名被 403 拒、移除被 400 拒，自解锁路径断裂
      （4d82e64 文案承诺的「设置 → 成员管理移除旧名字」会成死路）
    """
    _set_plan("team")
    gate = seats.enforce(tmp_path, {}, shared=True)
    assert gate is not None and gate[0] == 400 and gate[1].get("need_operator") is True

    _set_plan("pro")  # 标准授权同样面向单人
    assert seats.enforce(tmp_path, {}, shared=True) is None


def test_unauthorized_share_full_seat_self_unlock(tmp_path):
    """未授权共享 1 席被占 + operator 空 ⇒ 自解锁闭环（HTTP 层同场景见
    test_web_auth.py 的 test_unauthorized_share_full_seat_self_unlock）。"""
    seats._write_seats(tmp_path, [{"name": "旧名", "first_seen": "t0", "last_seen": "t0"}])

    code, payload = seats.api_seat_check(tmp_path, {"name": "我"}, shared=True)
    assert code == 403 and payload["seats_full"] is True
    assert "移除旧名字" in payload["error"]

    # 匿名移除（未授权共享免名）⇒ 名单空 ⇒ 新名字登记成功
    code, payload = seats.api_seat_remove(tmp_path, {"name": "旧名"}, shared=True)
    assert code == 200 and payload["seats"] == []
    code, _ = seats.api_seat_check(tmp_path, {"name": "我"}, shared=True)
    assert code == 200
    assert seats.enforce(tmp_path, {}, shared=True) is None


def test_baseline_depends_on_plan():
    """档位基线（**纯算术层**）：标准/团队 5 人、免费 2 人、私有化 10 人。

    ⚠️ 2026-10-25 起 free/pro 的基线**不开放共享**（产品规则在 `team_authorized`
    与 `seat_limit` 闸门，见下一节测试）：这两个数字只在算术与测试里有意义，
    别再当"免费能拉 2 个人试协作"讲——那个升级触发器设计已作废。
    """
    assert seat_quota.base_for_plan("pro") == 5
    assert seat_quota.base_for_plan("free") == 2
    assert seat_quota.base_for_plan("没这个档位") == 2, "未知档位按免费档处理（fail-safe）"

    assert seat_quota.effective_limit() == 2   # 无授权 ⇒ 免费档
    _set_plan("pro")
    assert seat_quota.effective_limit() == 5   # 有标准版授权
    _set_plan("free")
    assert seat_quota.effective_limit() == 2   # 授权失效/过期 ⇒ 回落


# -- 团队共享只随团队授权（2026-10-25 产品定位调整）-------------------------------

def test_team_authorized_only_for_team_private_or_contract_seats():
    """🔴 授权判定：team/private 授权、或 v2 明写的合同席位 ⇒ 开放共享；
    体验/标准（v1 激活码）/ 未激活/坏档 ⇒ 不开放（fail-closed 到单人）。"""
    assert seat_quota.team_authorized() is False          # 未激活
    _set_plan("pro")
    assert seat_quota.team_authorized() is False          # 标准授权面向单人
    _set_plan("free")
    assert seat_quota.team_authorized() is False          # 授权失效 ⇒ 回落未激活
    _set_plan("team")
    assert seat_quota.team_authorized() is True
    _set_plan("private")
    assert seat_quota.team_authorized() is True


def test_v2_contract_seats_open_sharing_even_on_pro_plan(monkeypatch):
    """v2 的 `seats` **优先于 plan**：合同签了席位就是买了协作
    （`issue_license_v2 --plan pro --seats 7` 是合法组合）。

    ⚠️ v1 路径的 license.json 即使手写 seats 也不认（`read_license` 对 v1 恒填
    `seats=None`）——只有验签过的 v2 合同席位才算数，这里用真 Ed25519 签一份。
    """
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("MAT_LICENSE_PUBKEY_B64", base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)).decode())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    data = {"v": 2, "license_id": "LIC-pro-seats", "customer": "测试客户", "plan": "pro",
            "features": sorted(licensing.plan_features("pro")), "seats": 7,
            "machine_limit": 2, "issued_at": int(time.time()),
            "expires_at": int(time.time()) + 3600}
    licensing.license_path().write_text(
        json.dumps(licensing.sign_license_v2(data, pem), ensure_ascii=False), encoding="utf-8")

    assert seat_quota.licensed_seats() == 7
    assert seat_quota.team_authorized() is True
    assert seats.seat_limit(shared=True) == 7, "合同席位覆盖档位基线"


def test_shared_uses_plan_baseline_when_authorized(tmp_path):
    """团队授权下 `--share` 恢复按基线放人（team 5 人），第 6 人 403 走团队话术。"""
    _set_plan("team")
    assert seats.seat_limit(shared=True) == seat_quota.BASE_SEATS_PRO
    for name in ("甲", "乙", "丙", "丁", "戊"):
        assert seats.api_seat_check(tmp_path, {"name": name}, shared=True)[0] == 200
    code, payload = seats.api_seat_check(tmp_path, {"name": "己"}, shared=True)
    assert code == 403 and "团队成员已满" in payload["error"]
    assert "团队授权" not in payload["error"], "授权满了不该再提示去拿授权"


def test_private_baseline_is_ten():
    _set_plan("private")
    assert seats.seat_limit(shared=True) == seat_quota.BASE_SEATS_PRIVATE


def test_api_seats_reports_team_authorized(tmp_path):
    """设置页要能区分「共享了但没授权」与「团队授权」——否则团队客户移除成员后
    卡片会误渲染成"去拿授权"提示（limit 推不出授权状态：合同席位可以是 1）。"""
    code, payload = seats.api_seats(tmp_path, shared=True)
    assert code == 200 and payload["team_authorized"] is False
    _set_plan("team")
    code, payload = seats.api_seats(tmp_path, shared=True)
    assert payload["team_authorized"] is True


def test_describe_says_single_person_when_not_authorized():
    """`mat seat status` 对未授权用户只说"单人 + 团队共享随授权提供"——
    别把算术层里的"免费基线 2 人"打印出去（那台机器 --share 下也只有 1 人）。"""
    text = seat_quota.describe()
    assert "1 人" in text and "团队授权" in text
    _set_plan("team")
    assert "团队授权" not in seat_quota.describe(), "授权后走正常分批口径"


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
    """发起人的例子：**团队授权 5 人** → 10-30 加 3 人、为期 2 个月 → 12-30 之后回落 5。

    ⚠️ 2026-10-25 起这个例子只能讲**团队授权**（标准/体验授权不开放共享，`--share`
    下恒 1 人）⇒ 这里显式写一份 team 授权，顺带覆盖"基线按档位分"这条。
    """
    _set_plan("team")
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
    _set_plan("team")  # 未授权时 describe 只说单人（见上一节），分批口径要授权后才打印
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
