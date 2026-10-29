"""离线模式（P0-4，2026-10-19）。

**为什么要有它**：产品承诺「数据不出本机」，但这句承诺此前只靠"代码恰好没在
run 路径调模型"维持，没有任何机制兜底。`MAT_OFFLINE=1` 把承诺变成可执行开关：

- 需要联网的动作（AI 生成/归因、激活登记、邀请、版本检查、webhook 通知）
  **在发起网络请求前**就被拒绝，给出可行动提示，而不是等 10s 超时；
- 本地执行/回放/自愈/证据报告**完全不受影响**——这正是产品的核心闭环；
- 私有化交付包（P2-3）默认写入 `MAT_OFFLINE=1`。

判定只看环境变量 `MAT_OFFLINE`（`1/true/yes/on` 均开）。零网络测试见
`tests/test_offline_guarantee.py`（socket 级断言 `mat run` 零出网）。

**本机端点放行**（2026-10-26）：离线模式承诺的是「数据不出本机」，而
`http://127.0.0.1` / `localhost` 上的服务不产生出网流量——把本地小模型（含本地
VL 兜底）一并拒掉，等于让「私有化交付包 + 本地模型」这一本该配套的组合不可用。
所以 `require_online(action, endpoint=...)` 在端点指向本机时直接放行；判定只看
host，不看端口（同机任意端口都在本机内）。
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit


class OfflineError(RuntimeError):
    """离线模式下拒绝了需要联网的操作；消息直接给用户看，要可行动。"""


_TRUTHY = {"1", "true", "yes", "on"}

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def is_offline() -> bool:
    """是否处于离线模式（`MAT_OFFLINE` ∈ {1,true,yes,on}）。"""
    return os.environ.get("MAT_OFFLINE", "").strip().lower() in _TRUTHY


def is_loopback(endpoint: str) -> bool:
    """端点是否指向本机（127.0.0.0/8、::1、localhost）。

    只看 host、不看端口——同机任意端口都在本机内。带 userinfo 或形如
    `127.0.0.1.evil.com` 的地址按**真实 host** 判定（`urlsplit` 已剥掉 userinfo），
    不会被前缀骗过去。
    """
    text = str(endpoint or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = "//" + text  # 容错：只给了 host:port / host
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:  # 非法 URL（如端口越界）按"非本机"处理，宁可拦错不可放错
        return False
    host = host.strip("[]").lower()
    if not host:
        return False
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_online(action: str, endpoint: str | None = None) -> None:
    """离线模式下抛 OfflineError；`endpoint` 指向本机时放行（本机不出网）。

    调用方把它当"明确的业务失败"处理，不要重试。
    """
    if endpoint and is_loopback(endpoint):
        return
    if is_offline():
        raise OfflineError(
            f"离线模式（MAT_OFFLINE=1）：{action} 需要联网，已按离线策略拒绝。"
            "本地执行/回放/自愈/报告不受影响；要恢复联网能力请移除 MAT_OFFLINE 或设为 0。"
            "（本机端点如 http://127.0.0.1:8080/v1 不受此限——不出网）")
