"""离线模式（P0-4，2026-10-19）与私有端点（2026-11-23）。

**为什么要有它**：产品默认口径是「内容只在本机处理」，但这句承诺此前只靠"代码恰好没在
run 路径调模型"维持，没有任何机制兜底。`MAT_OFFLINE=1` 把承诺变成可执行开关：

- 需要联网的动作（AI 生成/归因、激活登记、邀请、版本检查、webhook 通知）
  **在发起网络请求前**就被拒绝，给出可行动提示，而不是等 10s 超时；
- 本地执行/回放/自愈/证据报告**完全不受影响**——这正是产品的核心闭环；
- 私有化交付包（P2-3）默认写入 `MAT_OFFLINE=1`。

判定只看环境变量 `MAT_OFFLINE`（`1/true/yes/on` 均开）。零网络测试见
`tests/test_offline_guarantee.py`（socket 级断言 `mat run` 零出网）。

**本机端点放行**（2026-10-26）：离线模式守住的是「内容不经外网」，而
`http://127.0.0.1` / `localhost` 上的服务不产生出网流量——把本地小模型（含本地
VL 兜底）一并拒掉，等于让「私有化交付包 + 本地模型」这一本该配套的组合不可用。
所以 `require_online(action, endpoint=...)` 在端点指向本机时直接放行；判定只看
host，不看端口（同机任意端口都在本机内）。

**私有端点**（2026-11-23）：私有化部署的主路径通常不是本机回环，而是内网 GPU 机、
内网业务接口、内网 webhook、内网 matcloud。只认回环等于把这些全拒掉。所以引入
`MAT_PRIVATE_HOSTS`（逗号/空白分隔），并把「是否本机」升级为「是否私有端点」：

    is_private_endpoint(url) = 本机回环 ∪ MAT_PRIVATE_HOSTS

它同时决定两件事，不能拆开（否则会重演 2026-10-26 那类 bug）：
1. 离线模式放不放行；
2. 视觉兜底按不按「自托管模型」处理（本地协议、不占平台额度、不强制 API Key）。

条目写法（刻意收窄，避免把离线模式关掉）：
- IP 或网段：`192.168.1.9`、`10.0.0.0/8`、`fd00::/8`；
- 精确主机名：`gpu-gw.corp.local`；
- 点开头的后缀：`.corp.local`（匹配 `a.corp.local`，**不匹配** `evilcorp.local`）。
- **网段只匹配 IP 字面量，不做 DNS 解析**（防 DNS rebinding，判定可离线单测）。
- 拒绝：`*`、带协议/端口/`@` 的写法、`0.0.0.0/0` 与 `::/0`、只有一级的后缀
  （`.com`）、官方托管主机（`mobileaitest.com` 及其子域）。

**承诺的边界**：MAT 只保证自己的进程不连接私有端点之外的主机；私有端点自己是否
再往外转发（如 LiteLLM 网关转云端、内网 matcloud 配了云端模型），MAT 看不到，
由部署方负责。不要把「离线：开」读成「内容绝对不出内网」。
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class OfflineError(RuntimeError):
    """离线模式下拒绝了需要联网的操作；消息直接给用户看，要可行动。"""


_TRUTHY = {"1", "true", "yes", "on"}

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

#: 私有端点声明（2026-11-23）。改名过：不用 `MAT_OFFLINE_ALLOW_HOSTS`——它不只服务
#: 于离线闸门，也决定视觉兜底的协议与额度，叫「allow」会读成只跟离线有关。
ENV_PRIVATE_HOSTS = "MAT_PRIVATE_HOSTS"

#: 主机名/域名后缀的语法（单标签 1–63 字符，不以连字符开头或结尾）。
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")


def is_offline() -> bool:
    """是否处于离线模式（`MAT_OFFLINE` ∈ {1,true,yes,on}）。"""
    return os.environ.get("MAT_OFFLINE", "").strip().lower() in _TRUTHY


def host_of(endpoint: str) -> str:
    """端点的真实 host（小写、去方括号与 FQDN 尾点）；解析不了返回空串。

    从 `is_loopback` 里抽出来（行为保持一致）：`urlsplit` 已剥掉 userinfo，
    `127.0.0.1@evil.com` 取到的是 `evil.com`；非法 URL（端口越界等）返回空串，
    按「非私有」处理——宁可拦错，不可放错。
    """
    text = str(endpoint or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text  # 容错：只给了 host:port / host
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host.strip("[]").lower().rstrip(".")


def is_loopback(endpoint: str) -> bool:
    """端点是否指向本机（127.0.0.0/8、::1、localhost）。

    只看 host、不看端口——同机任意端口都在本机内。带 userinfo 或形如
    `127.0.0.1.evil.com` 的地址按**真实 host** 判定（`urlsplit` 已剥掉 userinfo），
    不会被前缀骗过去。
    """
    host = host_of(endpoint)
    if not host:
        return False
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class PrivateHosts:
    """`MAT_PRIVATE_HOSTS` 的解析结果。

    `invalid` 与 `rejected` 分开：前者是写法不对（改写法就能用），后者是写法对但
    不允许（如 `0.0.0.0/0`、官方托管主机）。`doctor` 会把两类都报出来。
    """

    networks: tuple = ()
    names: frozenset = frozenset()
    suffixes: tuple = ()
    invalid: tuple = ()    # ((原始条目, 原因), ...)
    rejected: tuple = ()   # ((原始条目, 原因), ...)

    def describe(self) -> list[str]:
        return [str(n) for n in self.networks] + sorted(self.names) + list(self.suffixes)


def _official_hosts() -> frozenset[str]:
    """官方托管主机（不能被声明为私有端点：会让视觉兜底改走本地协议 404，并跳过额度）。"""
    from .cloudapi import DEFAULT_CLOUD_URL  # 延迟导入：cloudapi 也会导入本模块

    return frozenset({host_of(DEFAULT_CLOUD_URL)})


def _covers_official(name: str = "", suffix: str = "") -> bool:
    for official in _official_hosts():
        if not official:
            continue
        if name and (name == official or name.endswith("." + official)):
            return True
        if suffix and ("." + official).endswith(suffix):
            return True
    return False


def parse_private_hosts(raw: str) -> PrivateHosts:
    """解析 `MAT_PRIVATE_HOSTS`。规则见模块 docstring，刻意收窄。"""
    networks: list = []
    names: set[str] = set()
    suffixes: list[str] = []
    invalid: list[tuple[str, str]] = []
    rejected: list[tuple[str, str]] = []
    for item in re.split(r"[,\s]+", raw or ""):
        entry = item.strip().lower().rstrip(".")
        if not entry:
            continue
        if "*" in entry or "://" in entry or "@" in entry:
            invalid.append((item, "只写主机名、IP 或网段，不带协议/端口/通配符"))
            continue
        is_ip_like = "/" in entry
        if not is_ip_like:
            try:
                ipaddress.ip_address(entry.strip("[]"))
                is_ip_like = True
            except ValueError:
                pass
        if is_ip_like:
            try:
                net = ipaddress.ip_network(entry.strip("[]"), strict=False)
            except ValueError:
                invalid.append((item, "IP/网段写法不对"))
                continue
            if net.prefixlen == 0:
                rejected.append((item, "前缀长度为 0，等于关闭离线模式"))
                continue
            networks.append(net)
            continue
        if entry.startswith("."):
            body = entry[1:]
            if "." not in body:
                rejected.append((item, "后缀太宽，至少两级（如 .corp.local）"))
            elif not _HOSTNAME_RE.match(body):
                invalid.append((item, "后缀写法不对"))
            elif _covers_official(suffix=entry):
                rejected.append((item, "不能覆盖官方托管主机"))
            else:
                suffixes.append(entry)
            continue
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", entry):
            # 四段纯数字但 ip_address 解析失败 ⇒ 写错的 IP。不能静默当主机名：
            # 那样 `999.999.999.999` 会被接受，用户以为声明了 IP、实际声明了一个名字。
            invalid.append((item, "IP 写法不对（四段都应是 0–255）"))
        elif ":" in entry:
            invalid.append((item, "不要带端口（判定不看端口）"))
        elif not _HOSTNAME_RE.match(entry):
            invalid.append((item, "主机名写法不对"))
        elif _covers_official(name=entry):
            rejected.append((item, "官方托管主机不能声明为私有端点"))
        else:
            names.add(entry)
    return PrivateHosts(tuple(networks), frozenset(names), tuple(suffixes),
                        tuple(invalid), tuple(rejected))


def private_hosts() -> PrivateHosts:
    """每次读环境变量（与 `is_offline` 同口径：`.env` 重载后立即生效，不缓存）。"""
    return parse_private_hosts(os.environ.get(ENV_PRIVATE_HOSTS, ""))


def is_private_endpoint(endpoint: str) -> bool:
    """本机回环，或命中 `MAT_PRIVATE_HOSTS`。

    语义 =「这台主机属于部署方自己的环境」。它同时决定：离线模式放不放行，以及
    视觉兜底按不按「自托管模型」处理（本地协议、不占平台额度）。
    """
    if is_loopback(endpoint):
        return True
    host = host_of(endpoint)
    if not host:
        return False
    ph = private_hosts()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        # IPv4-mapped IPv6（::ffff:10.1.2.3）按 IPv4 网段判，否则声明 10.0.0.0/8 漏掉它
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip.version == n.version and ip in n for n in ph.networks)
    return host in ph.names or any(host.endswith(s) for s in ph.suffixes)


def require_online(action: str, endpoint: str | None = None) -> None:
    """离线模式下抛 OfflineError；`endpoint` 是私有端点时放行。

    先判 `is_offline()`：非离线时直接返回，不做 `MAT_PRIVATE_HOSTS` 解析（每次
    请求都解析一遍没必要）。
    """
    if not is_offline():
        return
    if endpoint and is_private_endpoint(endpoint):
        return
    where = host_of(endpoint) if endpoint else ""
    raise OfflineError(
        f"离线模式（MAT_OFFLINE=1）：{action} 需要访问{where or '外部服务'}，已按离线策略拒绝。"
        "本地执行/回放/自愈/报告不受影响。"
        f"若该主机在你的内网，把它加进 {ENV_PRIVATE_HOSTS}"
        "（如 MAT_PRIVATE_HOSTS=10.0.0.0/8,gpu-gw.corp.local）；"
        "本机端点如 http://127.0.0.1:8080/v1 不受此限。"
        "要恢复联网能力请移除 MAT_OFFLINE 或设为 0。")
