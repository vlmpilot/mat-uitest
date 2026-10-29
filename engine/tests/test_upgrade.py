"""`mat upgrade` 的状态机：下单 → 轮询 → 领码 → 激活。

判据（都是真实用户会踩的路径）：
1. 付款成功 ⇒ 领码并激活，退出码 0；
2. 订单被关闭/退款 ⇒ 不领码，退出码 1；
3. 等待超时 ⇒ 给出「带令牌的领码命令」，不静默失败；
4. `--claim` 缺令牌 ⇒ 直接报用法，不发请求。
"""

from __future__ import annotations

import pytest

from insta_engine import cloudapi, licensing, upgrade

CODE = "MAT-pro.-0000-1111-22.aa-bbbb-c.dd-eeee-f"
ORDER = {
    "out_trade_no": "MAT20261028000001abcdef",
    "claim_token": "clm_0123456789abcdef0123456789abcdef",
    "subject": "MAT 标准版（月付）",
    "total_amount": "199.00",
    "payment_html": "<form/>",
}


class FakeApi:
    def __init__(self, states: list[str], code: str = CODE, default_state: str = "PAID"):
        self.states = list(states)
        self.default_state = default_state
        self.code = code
        self.calls: list[str] = []

    def post(self, path: str, payload: dict | None = None):
        self.calls.append(f"POST {path}")
        assert payload and payload["plan"] in ("pro_quarter", "pro_year", "pro_month")
        assert payload["machine_id"]
        return dict(ORDER)

    def get(self, path: str):
        self.calls.append(f"GET {path}")
        if path.startswith("/pay/orders/") and "/code" in path:
            return {"activation_code": self.code}
        return {"business_state": self.states.pop(0) if self.states else self.default_state}


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(licensing, "insta_home", lambda: tmp_path)
    return tmp_path


def test_upgrade_happy_path(fake_home):
    api = FakeApi(["PENDING_PAYMENT", "PENDING_PAYMENT", "PAID"])
    activated: list[str] = []

    rc = upgrade.run(plan="pro_month", open_browser=False, poll=0,
                     api=api, activate=lambda code: activated.append(code) or {
                         "plan": "pro", "expires_at": 1793000000})

    assert rc == 0
    assert activated == [CODE]
    assert any("/code?claim_token=" in c for c in api.calls)
    # 支付页要落地，用户随时能重开
    assert (fake_home / f"pay-{ORDER['out_trade_no']}.html").read_text(encoding="utf-8")


def test_upgrade_closed_order_returns_nonzero(fake_home):
    api = FakeApi(["CLOSED"])
    rc = upgrade.run(open_browser=False, poll=0, api=api,
                     activate=lambda code: pytest.fail("不应该激活"))
    assert rc == 1


def test_upgrade_timeout_prints_claim_hint(fake_home, capsys):
    api = FakeApi([], default_state="PENDING_PAYMENT")
    rc = upgrade.run(open_browser=False, poll=0, timeout=1, api=api,
                     activate=lambda code: pytest.fail("不应该激活"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "--claim" in out and ORDER["out_trade_no"] in out


def test_upgrade_claim_without_token(fake_home, capsys):
    api = FakeApi([])
    rc = upgrade.run(claim=ORDER["out_trade_no"], open_browser=False, api=api,
                     activate=lambda code: 0)
    assert rc == 1
    assert api.calls == []          # 缺令牌不该发请求
    assert "--token" in capsys.readouterr().out


def test_upgrade_claim_activates_other_machine(fake_home):
    api = FakeApi([])
    activated: list[str] = []
    rc = upgrade.run(claim=ORDER["out_trade_no"], token=ORDER["claim_token"],
                     open_browser=False, api=api,
                     activate=lambda code: activated.append(code) or {
                         "plan": "pro", "expires_at": 1793000000})
    assert rc == 0
    assert activated == [CODE]


def test_upgrade_survives_transient_query_error(fake_home):
    """轮询期间网络抖一下不该直接放弃（查询失败继续等，付款成功照样领码）。"""
    class FlakyApi(FakeApi):
        def get(self, path: str):
            if path.startswith("/pay/orders/") and "/code" not in path and not getattr(self, "_failed", False):
                self._failed = True
                raise cloudapi.CloudError("网络抖动")
            return super().get(path)

    api = FlakyApi(["PAID"])
    rc = upgrade.run(open_browser=False, poll=0, api=api,
                     activate=lambda code: {"plan": "pro", "expires_at": 1793000000})
    assert rc == 0
