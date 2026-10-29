"""`mat upgrade`：在终端里买、在终端里激活。

为什么需要这条命令：服务端的 `/pay/orders/{no}/code` 只是接口，不是给人用的。
如果让用户"下单 → 打开某个网页 → 复制一串码 → 再回来粘贴"，中间任何一步断了
都只能找人工。这里把五步串成一条：下单 → 打开支付页 → 轮询结果 → 领码 → 自动激活。

跨设备场景（付款机 ≠ 使用机）：付款机跑 `mat upgrade`，把终端里打印的
「订单号 + 领取令牌」带到使用机，执行
`mat upgrade --claim <订单号> --token <令牌>` 即可，不需要原机器参与。
"""

from __future__ import annotations

import time
import webbrowser
from pathlib import Path

#: 本地提示用的档位文案；**金额与标题一律以服务端返回为准**（客户端不做任何金额判断）。
PLAN_LABELS = {"pro_quarter": "标准版 · 3 个月卡", "pro_year": "标准版 · 年付",
               "pro_month": "标准版 · 月付"}

#: 轮询间隔：每次查询都会真的打支付宝网关，5 秒一次既够快也不会触发频控。
_POLL_SECONDS = 5.0


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _pay_page_path(out_trade_no: str) -> Path:
    from .licensing import insta_home

    path = insta_home() / f"pay-{out_trade_no}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _claim_code(api, out_trade_no: str, token: str) -> str:
    """领码：令牌是跨设备凭据，不要在这一层再补 machine_id（服务端二选一即可）。"""
    data = api.get(f"/pay/orders/{out_trade_no}/code?claim_token={token}")
    return str(data.get("activation_code") or "")


def _activate(code: str, activate) -> int:
    try:
        info = activate(code)
    except ValueError as exc:
        print(f"❌ 激活失败：{exc}")
        return 1
    if info.get("kept_existing"):
        print(f"ℹ️ 本机已有不更短的授权（至 {_day(info['expires_at'])}），本次未改动。")
    else:
        print(f"✅ 已激活 {info['plan']}（有效期至 {_day(info['expires_at'])}）")
    return 0


def run(plan: str = "pro_quarter", claim: str | None = None, token: str | None = None,
        open_browser: bool = True, timeout: int = 900,
        poll: float = _POLL_SECONDS, api=None, activate=None) -> int:
    """返回进程退出码（0 成功）。`api`/`activate` 只为测试注入，正常路径用默认实现。"""
    from . import cloudapi, licensing

    api = api or cloudapi
    activate = activate or licensing.activate

    if claim:
        if not token:
            print("用法：mat upgrade --claim <订单号> --token <领取令牌>"
                  "（令牌在下单那台机器的终端里打印过）")
            return 1
        try:
            code = _claim_code(api, claim, token)
        except cloudapi.CloudError as exc:
            print(f"❌ 领码失败：{exc}")
            return 1
        if not code:
            print("❌ 服务端没有返回激活码（订单可能未支付），请确认付款后重试")
            return 1
        print(f"激活码：{code}")
        return _activate(code, activate)

    mid = licensing.machine_id()
    try:
        order = api.post("/pay/orders", {"plan": plan, "machine_id": mid})
    except cloudapi.CloudError as exc:
        print(f"❌ 下单失败：{exc}")
        return 1

    out_trade_no = str(order["out_trade_no"])
    claim_token = str(order["claim_token"])
    page = _pay_page_path(out_trade_no)
    page.write_text(str(order["payment_html"]), encoding="utf-8")

    print(f"订单 {out_trade_no} · {order.get('subject')} · ¥{order.get('total_amount')}")
    print(f"支付页：{page}")
    if open_browser:
        try:
            webbrowser.open(page.as_uri())
            print("已在浏览器打开支付页，付款后会自动继续（Ctrl-C 可中断）。")
        except Exception:  # noqa: BLE001 - 打不开浏览器不是失败，页面路径已经给出来了
            print("没能自动打开浏览器，请手动打开上面的支付页。")
    print(f"跨设备：付款后可在别的机器执行 "
          f"`mat upgrade --claim {out_trade_no} --token {claim_token}`")

    deadline = time.time() + max(1, timeout)
    last_note = time.time()
    while time.time() < deadline:
        try:
            state = str(api.get(f"/pay/orders/{out_trade_no}").get("business_state") or "")
        except cloudapi.CloudError as exc:
            # 网络抖动不该直接放弃：记一句继续等，超时才算失败
            print(f"（查询失败，稍后重试：{exc}）")
            state = ""
        if state == "PAID":
            break
        if state in ("CLOSED", "REFUNDED"):
            print(f"❌ 订单已{'关闭' if state == 'CLOSED' else '退款'}，本次购买未完成。")
            return 1
        if time.time() - last_note > 30:
            print("等待付款…（付款完成后会自动继续）")
            last_note = time.time()
        time.sleep(poll)
    else:
        print("⏱ 等待超时。已付款的话，可用下面这条命令直接领码：")
        print(f"  mat upgrade --claim {out_trade_no} --token {claim_token}")
        return 1

    try:
        code = _claim_code(api, out_trade_no, claim_token)
    except cloudapi.CloudError as exc:
        print(f"❌ 领码失败：{exc}")
        return 1
    if not code:
        print("❌ 服务端暂未返回激活码，请稍后执行："
              f"mat upgrade --claim {out_trade_no} --token {claim_token}")
        return 1
    print(f"激活码：{code}")
    return _activate(code, activate)
