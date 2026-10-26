"""权益门控（P0-3，2026-10-19）。

**定位**：把授权从 `plan == "pro"` 的布尔判断升级为「功能集」判断——
Team/Private 与 Pro 卖的是**不同的东西**（协作/云端分享/审计 vs 离线交付），
代码里应该写 `require("team_share")`，而不是往 plan 里塞新档位判断。

功能集的**单一事实源**在 `licensing.py`（FEATURE_CORE/TEAM/PRIVATE）；
本模块只做读取封装与门控断言。门控哲学与配额一致：

- **本地核心能力永不加锁**（run/gen/heal/report/api_assert/...）——获客根基；
- 收费的是**协作（team_share）、云端分享（report_share）、审计（audit_log）、
  私有化交付（private_deploy/offline）**——这些是"用户自己搞不定/不想自己搞"的部分。

⚠️ 门控是**合同可执行性**，不是 DRM（与 seat_quota 同哲学）：改本地文件能绕开，
但那违反合同；对外承诺口径见 docs/实施方案 §3.2。
同理：license v2 的 `machine_limit` 是**合同条款**，不做本机技术锁定
（v1 激活码的机器上限由云端执行，两套模型口径不同，销售/客户沟通时写清）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .licensing import read_license


class EntitlementError(RuntimeError):
    """当前授权不含所需功能；消息直接给用户看，要可行动。"""


@dataclass(frozen=True)
class Entitlements:
    plan: str
    features: frozenset
    seats: int | None
    expires_at: int | None
    source: str  # "v2" | "v1" | "none"
    customer: str = ""
    license_id: str = ""
    error: str | None = None  # v2 坏档原因（malformed/expired/signature/unsupported）


def current() -> Entitlements:
    """读当前授权（licensing.read_license 已做验签/降级，这里只做形状封装）。"""
    lic = read_license()
    return Entitlements(
        plan=str(lic.get("plan") or "free"),
        features=frozenset(lic.get("features") or ()),
        seats=lic.get("seats"),
        expires_at=lic.get("expires_at"),
        source=str(lic.get("source") or "none"),
        customer=str(lic.get("customer") or ""),
        license_id=str(lic.get("license_id") or ""),
        error=lic.get("error"),
    )


def has(feature: str) -> bool:
    """当前授权是否含某功能；坏档（error）一律 False。"""
    ent = current()
    if ent.error:
        return False
    return feature in ent.features


def require(feature: str) -> None:
    """门控断言：无该功能抛 EntitlementError，文案给出档位与升级指向。"""
    ent = current()
    if feature in ent.features:
        return
    if ent.error:
        raise EntitlementError(
            f"license 校验失败（{ent.error}），无法使用「{feature}」——"
            "先解决授权问题（可重装 `pip install --force-reinstall mat-uitest`，或索取新 license 文件）")
    raise EntitlementError(
        f"当前套餐（{ent.plan}）不含「{feature}」——该能力随 Team/Private 档提供，"
        "档位与联系方式见 mobileaitest.com")
