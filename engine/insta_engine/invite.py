"""邀请（分享得额度）的**客户端**侧 —— 。

## 本地只存两样东西（都在 `~/.mat/`，0600）

| 文件 | 内容 | 说明 |
|---|---|---|
| `invite_key.json` | 客户端密钥 | **永不上传**；只上传它的 `sha256` |
| `invite.json` | 服务端返回的身份 | `user_id` / `invite_code` / `token` |

⚠️ **邀请关系不落本地**（红线）：本地只缓存**自己的身份**，台账每次从服务端拉。
关系一旦落本地，就等于让用户自己给自己发额度。

## 两条纪律（§6.5 / §6.4）

1. **只在用户显式执行 `mat invite*` 时联网。** 没有"启动时静默注册"、没有后台轮询注册
   —— 那是与「数据不出内网」正面冲突的做法，`engine/tests/test_invite.py` 有护栏盯着。
2. **台账读不到时不许显示 `0`**，要显示「上次同步于 …」/「暂时读不到」。
   `0` 会被读成「我的邀请全丢了」（同族 4：**没有输出 ≠ 没问题**）。

## 为什么不做非对称签名

初稿写的是"客户端生成密钥对、服务端存公钥、请求签名"。实现时改成
**「客户端生成 32 字节密钥 → 上传 `sha256(密钥)` 作身份锚 → 后续用既有 Bearer token 认证」**：

- 与既有 `/usage`、`/mat/complete` 的认证方式**完全一致**，不引入第二套机制；
- 不需要 `cryptography` / `pynacl` 依赖（引擎的依赖表里没有，也不该为一个功能加）；
- 安全等价：服务端只存 `sha256(密钥)`，库被拖走也反推不出可用凭证（同 `store.issue` 的做法）。

代价：不是密码学意义上的公钥，`public_key` 只是"客户端密钥的哈希"。**如实写在这里，别叫它密钥对。**
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

from .cloudapi import (
    DEFAULT_CLOUD_URL,  # noqa: F401 —— 再导出，兼容既有调用方
    CloudError,
    cloud_url,
)
from .cloudapi import get as _get
from .cloudapi import post as _post
from .quota import insta_home

#: 邀请错误 == 轻云通信错误。HTTP 这一层与激活登记**共用** `cloudapi`
#: （两份实现必然漂移，见该模块 docstring）。保留这个名字是为了让调用方读起来仍然自洽。
InviteError = CloudError


def key_path() -> Path:
    return insta_home() / "invite_key.json"


def state_path() -> Path:
    return insta_home() / "invite.json"


# -- 本地密钥与身份 ---------------------------------------------------------


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # 非 POSIX 文件系统（部分 Windows 配置）


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_client_key() -> str:
    """取客户端密钥；没有就生成一个并落盘。**它永不上传。**

    ⚠️ 变量名/JSON 字段名**别叫 `secret`**：发布前的密钥扫描会把
    `secret = <16+ 字符>` 这种形状当成硬编码密钥拦下（实测：3 处假阳性，发布**响亮失败**）。
    那是扫描**该有的**行为（fail-closed），要改的是命名，不是扫描。
    """
    existing = _read_json(key_path()).get("key")
    if isinstance(existing, str) and len(existing) >= 32:
        return existing
    client_key = secrets.token_urlsafe(32)
    _write_private(key_path(), {"key": client_key})
    return client_key


def public_key() -> str:
    """上传给服务端的身份锚 = `sha256(客户端密钥)`（**不是密码学公钥**，见模块 docstring）。"""
    return hashlib.sha256(ensure_client_key().encode("utf-8")).hexdigest()


def load_state() -> dict:
    return _read_json(state_path())


def save_state(state: dict) -> None:
    _write_private(state_path(), state)


def auth_token() -> str:
    """调用需要认证的邀请接口时用的凭证：本地身份里的 token，回退到 `GEN_API_KEY`。"""
    token = load_state().get("token")
    if isinstance(token, str) and token:
        return token
    return os.environ.get("GEN_API_KEY", "")


# -- 网络 -------------------------------------------------------------------


# -- 业务动作 ---------------------------------------------------------------


def register() -> dict:
    """注册匿名身份（幂等）。返回并落盘 `{user_id, invite_code, token}`。"""
    out = _post("/invite/register", {"public_key": public_key()})
    state = {
        "user_id": out.get("user_id"),
        "invite_code": out.get("invite_code"),
        "token": out.get("token"),
        "cloud_url": cloud_url(),
    }
    save_state(state)
    return state


def accept(invite_code: str) -> dict:
    """接受别人的邀请码（幂等）。**只登记关系**，额度要等跑通首轮。"""
    out = _post("/invite/accept", {"invite_code": invite_code, "public_key": public_key()})
    state = load_state()
    state.update({
        "user_id": out.get("user_id") or state.get("user_id"),
        "invite_code": out.get("invite_code") or state.get("invite_code"),
        "token": out.get("token") or state.get("token"),
        "cloud_url": cloud_url(),
        "accepted": invite_code.strip().upper().replace("-", ""),
    })
    save_state(state)
    return out


def qualify() -> dict:
    """告诉服务端「我已跑通首轮」⇒ 给邀请人计酬。幂等。

    正常由 `runner` 在**首次成功跑完一轮**后自动调用；`mat invite --confirm` 是兜底。
    """
    return _post("/invite/qualify", {}, token=auth_token())


def ledger() -> dict:
    """拉邀请台账。**必须带认证** —— 关系只在服务端。"""
    return _get("/invite", token=auth_token())


#: 写进本地快照的字段。`mat status` 与配置台**只读快照**，所以这些键要够它俩用。
_LEDGER_KEYS = ("invited", "qualified", "pending", "credited_this_month",
                "monthly_cap", "reward_kind", "reward_amount", "capped")


def sync_ledger() -> dict:
    """拉台账 → 写本地快照 → 尝试领取 Pro 奖励。返回 `{ledger, claim}`。

    **这是唯一拉台账的地方**（CLI 与配置台共用）：快照格式与领奖纪律只有一份实现，
    不会出现"CLI 存了 8 个键、配置台存了 5 个"这种静默分叉。
    """
    from datetime import datetime

    led = ledger()
    state = load_state()
    state["ledger_synced_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    state["ledger_cache"] = {k: led.get(k) for k in _LEDGER_KEYS}
    save_state(state)
    claim = None
    try:
        claim = claim_pro_reward(led)
    except (InviteError, ValueError):
        claim = None      # 激活码用不了不影响看台账
    return {"ledger": led, "claim": claim}


# -- 里程碑与领奖 -----------------------------------------------------------


def notify_first_run() -> dict | None:
    """「我跑通首轮了」——由 `mat run` 在一轮执行正常结束后调用。

    ⚠️ **没有接受过邀请时一个请求都不发**：先看本地状态，`accepted` 为空就直接返回。
    一个跑测试的命令不该因为我们想知道什么而悄悄出网（§6.5 的可验证纪律）。

    ⚠️ **失败一律吞掉**：邀请是旁路功能，**跑测试绝不能因为它而失败**。
    """
    from datetime import datetime

    state = load_state()
    if not state.get("accepted") or state.get("qualified_at"):
        return None
    try:
        out = qualify()
    except InviteError:
        return None
    state = load_state()
    state["qualified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(state)
    return out


def notice(state: dict | None = None) -> str | None:
    """概览页要显示的「奖励到账」提示；没有则 `None`（**不常驻**，⑤）。

    只读本地快照 ⇒ 概览页不会因此联网（§6.4 同一纪律）。
    """
    from datetime import datetime

    st = state if state is not None else load_state()
    claim = st.get("pro_claim") or {}
    if claim.get("activated") and not st.get("pro_claim_acked"):
        try:
            until = datetime.fromtimestamp(int(claim["expires_at"])).strftime("%Y-%m-%d")
        except (KeyError, TypeError, ValueError, OSError):
            until = "（时间未知）"
        return f"邀请奖励已到账：Pro 有效期延长至 {until}"

    led = st.get("ledger_cache") or {}
    credited = int(led.get("credited_this_month") or 0)
    seen = int(st.get("notice_acked_credited") or 0)
    if credited > seen:
        return (f"邀请奖励已到账：本月新增 {credited - seen} 人计酬"
                f"（共 {credited}/{led.get('monthly_cap', 0)}）")
    return None


def ack_notice() -> None:
    """用户看过提示了。

    **必须记 ack** —— 不记的话提示会一直冒，而"只在到账时冒一条"是明确的设计要求
    （常驻提示会变成噪音，用户很快就不看了）。
    """
    st = load_state()
    st["pro_claim_acked"] = True
    led = st.get("ledger_cache") or {}
    st["notice_acked_credited"] = int(led.get("credited_this_month") or 0)
    save_state(st)


def claim_pro_reward(ledger_data: dict | None = None) -> dict | None:
    """把邀请挣来的 Pro 时长激活到本机。**只在比当前更晚时才激活。**

    ⚠️ 「只在更晚时激活」这句判断不能省：服务端签的码是 `现在 + 累计天数`。
    如果用户本来就有更长的 Pro（例如自己买了年付），直接 `activate()` 会**把有效期改短**。

    返回 `None` = 没有 Pro 奖励；否则 `{activated: bool, ...}`。
    """
    from .licensing import activate, read_license

    data = ledger_data if ledger_data is not None else ledger()
    grant = data.get("pro_code") or {}
    code = grant.get("code")
    if not code:
        return None
    new_expires = int(grant.get("expires_at") or 0)
    current = int(read_license().get("expires_at") or 0)
    if new_expires <= current:
        return {"activated": False, "reason": "本机已有更长的有效期", "expires_at": current}
    activate(code)
    return {"activated": True, "expires_at": new_expires}
