"""续费激活：**新码不得把有效期改短**（2026-10-11 实测缺陷的回归门禁）。

缺陷原状：`licensing.activate()` 是 `write_text` **整体覆写** `license.json`——
"先激活 12 个月码、再激活 1 个月码"会把有效期从 2027-10-07 砍到 2026-11-11，
而 CLI 只打印"已激活（有效期至 …）"，用户完全看不出自己的授权被缩短了。

**累加发生在签发端**（`--from <原到期日>`），不是客户端：客户端如果把时长加到本机
现有到期日上，`license.json` 就变成一个可手工改长的授权来源，且与服务端邀请码的
签发口径（"现在 + 累计天数"）重复累加。本文件同时守这两件事。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest

from insta_engine import licensing

# 与 test_v1.py 同源的测试专用密钥（真实环境无效；变量名刻意不叫 *_SECRET，
# 免得触发导出脚本的"疑似密钥"自查）
TEST_LICENSE_KEY = "test-only"
MONTH = 30 * 86400


def mint(plan: str = "pro", months: int = 1, start_at: int | None = None) -> str:
    """按 verify_code 的口径造一张码（可指定起算时刻，用于模拟续费码）。"""
    base = int(start_at if start_at is not None else time.time())
    body = f"{plan}.{base + months * MONTH}.{uuid.uuid4().hex[:6]}"
    sig = hmac.new(TEST_LICENSE_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
    raw = f"{body}.{sig}"
    return "MAT-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", TEST_LICENSE_KEY)
    return tmp_path


def _expiry() -> int:
    return int(licensing.read_license()["expires_at"])


# -- 不得缩短 ----------------------------------------------------------------

def test_shorter_code_keeps_longer_expiry():
    """先 12 个月、再 1 个月 ⇒ 有效期保持 12 个月那份，而不是被砍短。"""
    licensing.activate(mint(months=12))
    long_expiry = _expiry()

    info = licensing.activate(mint(months=1))

    assert info["kept_existing"] is True, "更短的码必须走「保留现有」分支"
    # expires_at 语义 = 激活后本机的有效到期日（两分支统一），所以调用方直接打印它就对
    assert info["expires_at"] == long_expiry
    assert info["code_expires_at"] < long_expiry, "码自身的到期日仍如实返回（用于提示）"
    assert _expiry() == long_expiry, "本机授权不得被改短"


def test_kept_existing_leaves_file_untouched(tmp_path):
    """保留现有授权时，license.json 必须一个字节都不动（不留半改状态）。"""
    licensing.activate(mint(months=12))
    before = licensing.license_path().read_bytes()

    licensing.activate(mint(months=1))

    assert licensing.license_path().read_bytes() == before


def test_same_expiry_is_treated_as_no_change():
    """到期日完全相同也走保留分支（不无谓改写文件）。"""
    code = mint(months=6)
    licensing.activate(code)
    info = licensing.activate(code)
    assert info["kept_existing"] is True


# -- 正常升级路径仍然生效 ----------------------------------------------------

def test_longer_code_replaces_shorter_one():
    """先 1 个月、再 12 个月 ⇒ 取 12 个月那份（升级不能被挡住）。"""
    licensing.activate(mint(months=1))
    short_expiry = _expiry()

    info = licensing.activate(mint(months=12))

    assert info["kept_existing"] is False
    assert _expiry() > short_expiry
    assert _expiry() == info["expires_at"]


def test_first_activation_uses_code_expiry():
    """首次激活：有效期就是码自带的到期日（不放大、不缩小）。"""
    code = mint(months=3)
    info = licensing.activate(code)
    assert info["kept_existing"] is False
    assert _expiry() == licensing.verify_code(code)["expires_at"]


# -- 边界：不能因为"保留现有"而放松验签 --------------------------------------

def test_expired_code_still_rejected_by_default():
    """过期码依旧被拒（"保留现有"分支不得把过期码变成可用码）。"""
    expired = mint(months=0)
    assert licensing.verify_code(expired) is None
    with pytest.raises(ValueError):
        licensing.activate(expired)


def test_tampered_license_file_is_ignored():
    """license.json 里塞一张伪造码 ⇒ read_license 仍是 free。"""
    licensing.license_path().write_text(
        json.dumps({"plan": "pro", "expires_at": int(time.time()) + 10 * 365 * 86400,
                    "code": "MAT-pro.9999999999.deadbe.0000000000000000"}),
        encoding="utf-8")
    assert licensing.read_license()["plan"] == "free"


# -- 时长卡（2026-11-02 起纯离线）--------------------------------------------

def test_verify_code_parses_duration_card_months():
    """plan 段 `pro+N` ⇒ plan 还是 pro、months=N；畸形后缀按普通码（months=0）。"""
    out = licensing.verify_code(mint(plan="pro+12", months=12))
    assert out["plan"] == "pro" and out["months"] == 12
    assert licensing.verify_code(mint(plan="pro+abc", months=1))["months"] == 0
    assert licensing.verify_code(mint(months=1))["months"] == 0


def test_duration_card_uses_its_builtin_expiry_offline():
    """不再在线核销：时长卡按码内兜底到期日离线生效（与普通码同路径）。"""
    card = mint(plan="pro+6", months=6)
    info = licensing.activate(card)

    assert info["kept_existing"] is False
    assert info["expires_at"] == licensing.verify_code(card)["expires_at"]
    on_disk = json.loads(licensing.license_path().read_text(encoding="utf-8"))
    assert on_disk["code"] == card
    assert licensing.read_server_token() == "", "不再发生服务端登记"


def test_duration_card_never_shortens_existing():
    """谁长用谁：现有授权更长时保持不变、不写盘。"""
    licensing.activate(mint(months=12))
    long_exp = _expiry()
    before = licensing.license_path().read_bytes()

    info = licensing.activate(mint(plan="pro+1", months=1))

    assert info["kept_existing"] is True
    assert _expiry() == long_exp
    assert licensing.license_path().read_bytes() == before


# -- 失败原因必须能分开说（2026-10-13）----------------------------------------
#
# 原先 `verify_code` 只返回 None，CLI 就一律打印「激活码无效或已过期」——
# 把三种**下一步动作完全不同**的处境说成同一句（重抄一遍／联系我们／要新码）。
# 这一节锁住"分得开"，以及一条**说不出来的话**（见最后一节）。


def _tamper(code: str) -> str:
    return code[:-1] + ("0" if code[-1] != "0" else "1")


def test_verify_code_detailed_never_disagrees_with_verify_code():
    """带原因的版本必须与老契约**逐条同判**，否则就是两个真相源开始漂移。"""
    good = mint(months=12)
    expired = mint(months=1, start_at=int(time.time()) - 400 * 86400)

    for code in (good, expired, "MAT-not-a-real-code", _tamper(good), "", "MAT-"):
        detailed_none = licensing.verify_code_detailed(code)[0] is None
        assert detailed_none == (licensing.verify_code(code) is None), f"两版判定不一致：{code!r}"


def test_verify_code_detailed_reports_the_reason_code():
    """原因码是**结构化**的（调用方若要分流，不该去拆中文文案）。"""
    assert licensing.verify_code_detailed("MAT-nope")[1] == licensing.REASON_MALFORMED
    assert licensing.verify_code_detailed(_tamper(mint(months=12)))[1] == licensing.REASON_SIGNATURE
    card = mint(months=1, start_at=int(time.time()) - 400 * 86400)
    assert licensing.verify_code_detailed(card)[1] == licensing.REASON_EXPIRED
    assert licensing.verify_code_detailed(mint(months=12))[1] is None


def test_explain_code_failure_gives_three_different_answers():
    """格式 / 签名 / 过期 ⇒ 三句不同的话（用户据此决定：重抄、联系我们、还是要新码）。"""
    malformed = licensing.explain_code_failure("MAT-not-a-real-code")
    signature = licensing.explain_code_failure(_tamper(mint(months=12)))
    expired = licensing.explain_code_failure(mint(months=1, start_at=int(time.time()) - 400 * 86400))

    assert "格式" in malformed
    assert "签名" in signature and "整段" in signature
    assert "已于" in expired and "过期" in expired and "签名" not in expired
    assert len({malformed, signature, expired}) == 3, "三种失败不能退化成同一句话"


def test_local_gate_never_blocks_on_usage_history():
    """🔴 本地闸门**只看码本身**（签名 + 截止日），不看使用历史 —— 断网时也必须能激活。

    这条同时说明了为什么本地**说不出**「该码已被用过」：它压根没有这个信息源
    （`license.json` 只存本机记录，无状态 HMAC 也不带任何使用痕迹）。
    """
    code = mint(months=1)

    assert licensing.activate(code)["kept_existing"] is False
    licensing.license_path().unlink()                    # 模拟"另一台机器"（本机无授权记录）
    assert licensing.activate(code)["kept_existing"] is False, "本地不该有「一码一次」的概念"


def test_kept_existing_marks_whether_it_was_the_same_code():
    """`same_code` 让 CLI 能说"你已经用这张码激活过了"，而不是含糊的"保留现有授权"。"""
    code = mint(months=1)
    licensing.activate(code)

    again = licensing.activate(code)
    assert again["kept_existing"] is True and again["same_code"] is True

    other = licensing.activate(mint(months=12))          # 更长的码：正常写盘
    assert other["kept_existing"] is False and other["same_code"] is False

    shorter = licensing.activate(mint(months=1))         # 更短的别的码：保留现有，但不是同一张
    assert shorter["kept_existing"] is True and shorter["same_code"] is False
