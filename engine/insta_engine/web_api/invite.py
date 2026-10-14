"""邀请/分享的 HTTP 处理器（从 main 的 `web_server.py` 迁入，配合 `web_api/` 分层）。

⚠️ 迁入时只改了一处：`from . import invite` → `from .. import invite`（层级下移一层）。
其余逐字保留 —— 这两个处理器的纪律是**故意的**：

- `api_invite_status`：**只读本地，绝不联网**（状态页要快、要能离线）；
- `api_invite_action`：**唯一会联网的地方**，且 `join` 必须带 `agree: true`
  （匿名注册会新建服务端状态，用户必须先看到说明并同意）。
  **少这个字段就拒绝** —— 免得以后有人在别处顺手调用它，把"静默注册"从后门带回来。

🔴 **回给前端的报文一律剥掉凭证**（2026-10-14 加）：凭证只有一个出口，就是
`mat token`。理由见 `_scrub_credentials`。
"""

from __future__ import annotations

#: 报文里**永不出现在浏览器侧**的字段：
#: `token` = 匿名身份凭证；`pro_code` = 邀请奖励的**激活码**（它也是一张凭证）。
_CREDENTIAL_KEYS = ("token", "pro_code")


def _scrub_credentials(value):
    """递归剥掉凭证字段。

    🔴 **为什么要做在这一层**：控制台可能跑在 `mat --share` 下 ——
    **同网段任何一台浏览器都能打开它**。凭证不该出现在一个页面的响应里。
    （当前前端确实没有消费它们的地方，但"现在没人用"正是"以后有人顺手用一下"的温床。）

    剥掉不影响取用：`join` / `accept` 已经把凭证**落盘**到 `~/.mat/invite.json`，
    客户端要用时自己读本地；`pro_code` 的核销也在 `sync_ledger()` 内**服务端侧**完成，
    不需要经过浏览器。
    """
    if isinstance(value, dict):
        return {k: _scrub_credentials(v) for k, v in value.items() if k not in _CREDENTIAL_KEYS}
    if isinstance(value, list):
        return [_scrub_credentials(v) for v in value]
    return value


def api_invite_status() -> tuple[int, dict]:
    """邀请状态（**只读本地，绝不联网**）。

    台账数字来自本地快照 —— 与 `mat status` 同源同纪律：状态页要快、要能离线，
    联网只能由用户**显式点击**触发（`api_invite_action`）。
    """
    from .. import invite

    state = invite.load_state()
    code = state.get("invite_code")
    return 200, _scrub_credentials({
        "joined": bool(code),
        "invite_code": f"{str(code)[:4]}-{str(code)[4:]}" if code else None,
        "cloud_url": invite.cloud_url(),
        "accepted": state.get("accepted"),
        "ledger": state.get("ledger_cache") or None,
        "synced_at": state.get("ledger_synced_at"),
        "pro_claim": state.get("pro_claim"),
        # 概览页的「奖励到账」提示（不常驻；用户点"知道了"后由 action=ack 清掉）
        "notice": invite.notice(state),
    })


def api_invite_action(body: dict) -> tuple[int, dict]:
    """邀请动作 —— **唯一会联网的地方**，且必须由用户在界面上点出来。"""
    from .. import invite

    action = str(body.get("action", ""))
    try:
        if action == "join":
            if not body.get("agree"):
                return 400, {"error": "请先阅读并确认「将向 MAT 官方服务注册匿名 ID」的说明"}
            result = invite.register()
        elif action == "accept":
            code = str(body.get("code", "")).strip()
            if not code:
                return 400, {"error": "缺少邀请码"}
            result = invite.accept(code)
        elif action == "sync":
            result = invite.sync_ledger()
        elif action == "ack":
            # 纯本地动作（**不联网**）：把概览页那条到账提示标记为已读
            invite.ack_notice()
            result = {"acked": True}
        else:
            return 400, {"error": f"未知动作 {action!r}"}
    except invite.InviteError as exc:
        return 400, {"error": str(exc)}
    # 凭证只回给**本机终端**（`mat token`），不回给浏览器 —— 见 `_scrub_credentials`。
    return 200, {"ok": True, "result": _scrub_credentials(result)}
