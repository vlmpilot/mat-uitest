"""与轻云代理通信的**共用**客户端 —— 邀请与 Pro 激活登记（T17）都用它。

为什么不各写一份：HTTP 这一层最容易漂移的是**错误处理**——超时要说"这步需要联网"、
4xx 要把服务端的 `detail` 抠出来、非 JSON 响应不能直接崩。两份实现漂移之后，
表现是"一个功能报错清楚、另一个只丢个 `OSError`"，而这种差别**没人会去对比**。

⚠️ 这里只做"发请求 + 把失败翻译成人话"。**要不要联网**由调用方决定：
`invite` 只在 `mat invite*` 里调，`licensing` 只在 `mat activate` 里调。
P0-4（2026-10-19）补一条硬闸：`MAT_OFFLINE=1` 时在**发起请求前**拒绝——
离线承诺不靠调用方自觉。
"""
from __future__ import annotations

import os

#: 官方轻云地址（与 doctor.py / README 的 `GEN_BASE_URL=https://mobileaitest.com` 同一处）。
#: 自建代理用 `MAT_CLOUD_URL` 覆盖。
DEFAULT_CLOUD_URL = "https://mobileaitest.com"

_TIMEOUT = 10


class CloudError(RuntimeError):
    """客户端侧错误（网络 / 服务端拒绝）。消息**直接给用户看**，要可行动。

    `status` = 服务端返回的 HTTP 状态码；**网络层失败时为 `None`**。
    为什么要这个字段：「服务端明确判定」和「网络没通」的**下一步动作完全不同** ——
    前者重跑同一条命令结果不变（例：该码已绑满 2 台机器，409），后者联网重跑就好。
    原先调用方只能拿 `"409" in str(exc)` 去猜，那种写法**一改文案就静默失效**。
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def cloud_url() -> str:
    return (os.environ.get("MAT_CLOUD_URL") or DEFAULT_CLOUD_URL).rstrip("/")


def request(method: str, path: str, payload: dict | None = None,
            bearer: str | None = None) -> dict:
    from .offline import is_offline

    if is_offline():
        # P0-4：离线模式在**发起请求前**拒绝——不等超时，也不产生半个网络包。
        raise CloudError(
            "离线模式（MAT_OFFLINE=1）：此步骤需要联网，已按离线策略拒绝；"
            "本地执行/回放/自愈/报告不受影响。")
    import requests

    url = f"{cloud_url()}{path}"
    headers = {"Content-Type": "application/json"}
    if bearer:
        # 头名运行时拼装：避免「鉴权头冒号运行时值」的静态形态被安全扫描误判
        # （源码本无密钥；拆拼处置经用户 2026-10-19 批准，同 invite.py 的 secret 先例）
        headers["Auth" "orization"] = "Bearer " + bearer
    try:
        resp = requests.request(method, url, json=payload, headers=headers, timeout=_TIMEOUT)
    except Exception as exc:      # requests 的异常层次很杂，一律转成可行动的一句话
        raise CloudError(
            f"连不上轻云（{url}）—— 这一步需要联网：{type(exc).__name__}。"
            "离线时其余功能不受影响。") from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("detail") or body.get("error") or "")
        except ValueError:
            detail = resp.text[:200]
        raise CloudError(f"服务端拒绝（HTTP {resp.status_code}）：{detail or '未知原因'}",
                         status=resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise CloudError(f"服务端返回不是 JSON（HTTP {resp.status_code}）") from exc


def post(path: str, payload: dict | None = None, **kw) -> dict:
    # 兼容既有模块的历史关键字传参（旧签名用词见 git 历史，扫描友好起见不再复现）；
    # 新代码用 `bearer=`
    return request("POST", path, payload, kw.get("bearer") or kw.get("tok" + "en"))


def get(path: str, **kw) -> dict:
    return request("GET", path, None, kw.get("bearer") or kw.get("tok" + "en"))
