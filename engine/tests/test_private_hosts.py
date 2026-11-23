"""私有端点判定：`MAT_PRIVATE_HOSTS` 的解析与匹配（2026-11-23）。

设计原则（改这个文件前先读 `offline.py` 模块 docstring）：
- 私有端点 = 本机回环 ∪ `MAT_PRIVATE_HOSTS`，它同时决定「离线放不放行」与
  「视觉兜底按不按自托管处理」，必须用同一个函数；
- 规则刻意收窄：网段只匹配 IP 字面量（不做 DNS 解析）、后缀必须带点且至少两级、
  官方托管主机不能被声明。任何一条放宽都等于给离线承诺开洞。
"""

from __future__ import annotations

import pytest

from insta_engine.offline import (
    ENV_PRIVATE_HOSTS,
    OfflineError,
    host_of,
    is_loopback,
    is_private_endpoint,
    parse_private_hosts,
    private_hosts,
    require_online,
)

# -- host_of：与 is_loopback 共用同一套解析 ------------------------------------


@pytest.mark.parametrize("endpoint,expected", [
    ("http://127.0.0.1:8080/v1", "127.0.0.1"),
    ("127.0.0.1:8899/v1", "127.0.0.1"),          # 不带 scheme
    ("http://[::1]:8080/v1", "::1"),             # IPv6 方括号
    ("http://Example.COM./v1", "example.com"),   # 大小写 + FQDN 尾点
    ("http://127.0.0.1@evil.com/v1", "evil.com"),  # userinfo 骗术
    ("", ""),
    ("http://host:99999/", "host"),              # hostname 不校验端口（与旧 is_loopback 一致）
])
def test_host_of(endpoint, expected):
    assert host_of(endpoint) == expected


def test_loopback_behavior_unchanged_after_extraction():
    """`host_of` 是从 `is_loopback` 抽出来的，行为必须逐字不变。"""
    assert is_loopback("http://127.0.0.1:8080/v1")
    assert is_loopback("http://localhost:11434/v1")
    assert is_loopback("http://[::1]:8080/v1")
    for fake in ("http://127.0.0.1.evil.com/v1", "http://127.0.0.1@evil.com/v1",
                 "https://localhost.evil.com/v1", ""):
        assert is_loopback(fake) is False


# -- parse_private_hosts：接受与拒绝 ------------------------------------------


def test_parses_networks_names_and_suffixes():
    ph = parse_private_hosts("10.0.0.0/8, 192.168.1.9, fd00::/8, gpu-gw.corp.local, .corp.local")
    assert len(ph.networks) == 3
    assert ph.networks[0].prefixlen == 8
    assert "gpu-gw.corp.local" in ph.names
    assert ph.suffixes == (".corp.local",)
    assert not ph.invalid and not ph.rejected


@pytest.mark.parametrize("entry,reason_kind", [
    ("0.0.0.0/0", "rejected"),
    ("::/0", "rejected"),
    (".com", "rejected"),               # 只有一级，等于放行半个互联网
    (".local", "rejected"),
    ("mobileaitest.com", "rejected"),   # 官方托管主机
    (".mobileaitest.com", "rejected"),
    ("*", "invalid"),
    ("https://10.0.0.1", "invalid"),
    ("10.0.0.1:8080", "invalid"),       # 判定不看端口
    ("a@b", "invalid"),
    ("bad_host!", "invalid"),
    ("999.999.999.999", "invalid"),
])
def test_rejects_dangerous_or_malformed_entries(entry, reason_kind):
    ph = parse_private_hosts(entry)
    bucket = getattr(ph, reason_kind)
    assert bucket, f"{entry} 应当被 {reason_kind} 拒绝"
    assert not ph.networks and not ph.names and not ph.suffixes


def test_empty_env_means_no_private_hosts(monkeypatch):
    monkeypatch.delenv(ENV_PRIVATE_HOSTS, raising=False)
    ph = private_hosts()
    assert not ph.networks and not ph.names and not ph.suffixes


# -- is_private_endpoint：匹配语义 --------------------------------------------


def test_loopback_is_private_even_without_env(monkeypatch):
    monkeypatch.delenv(ENV_PRIVATE_HOSTS, raising=False)
    assert is_private_endpoint("http://127.0.0.1:8080/v1")
    assert is_private_endpoint("http://[::1]:11434/v1")


def test_network_matches_ip_literal_only(monkeypatch):
    """网段只匹配 IP 字面量——不做 DNS 解析（防 rebinding，判定可离线单测）。"""
    monkeypatch.setenv(ENV_PRIVATE_HOSTS, "10.0.0.0/8")
    assert is_private_endpoint("http://10.1.2.3:8000/v1")
    assert not is_private_endpoint("http://10-1-2-3.corp.local/v1")  # 主机名不解析
    assert not is_private_endpoint("http://11.0.0.1/v1")


def test_ipv4_mapped_ipv6_matches_ipv4_network(monkeypatch):
    monkeypatch.setenv(ENV_PRIVATE_HOSTS, "10.0.0.0/8")
    assert is_private_endpoint("http://[::ffff:10.1.2.3]:8000/v1")


def test_hostname_and_suffix_matching(monkeypatch):
    monkeypatch.setenv(ENV_PRIVATE_HOSTS, "gpu-gw.corp.local,.corp.local")
    assert is_private_endpoint("http://gpu-gw.corp.local:8000/v1")
    assert is_private_endpoint("http://a.corp.local/v1")
    # 关键：后缀带点，evilcorp.local 不能被 .corp.local 匹配
    assert not is_private_endpoint("http://evilcorp.local/v1")
    assert not is_private_endpoint("http://corp.local/v1")


def test_public_host_is_not_private(monkeypatch):
    monkeypatch.setenv(ENV_PRIVATE_HOSTS, "10.0.0.0/8")
    assert not is_private_endpoint("https://api.example.com/v1")
    assert not is_private_endpoint("https://mobileaitest.com/v1")


# -- require_online：行为 ------------------------------------------------------


def test_offline_allows_private_and_blocks_public(monkeypatch):
    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.setenv(ENV_PRIVATE_HOSTS, "10.0.0.0/8")
    require_online("内网模型", endpoint="http://10.1.2.3:8000/v1")  # 不抛
    with pytest.raises(OfflineError, match="离线模式"):
        require_online("公网接口", endpoint="https://api.example.com/v1")


def test_offline_error_names_the_env_var(monkeypatch):
    """报错要可行动：告诉用户把主机加进哪个变量。"""
    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.delenv(ENV_PRIVATE_HOSTS, raising=False)
    with pytest.raises(OfflineError, match=ENV_PRIVATE_HOSTS):
        require_online("公网接口", endpoint="https://api.example.com/v1")


def test_online_mode_never_blocks(monkeypatch):
    monkeypatch.delenv("MAT_OFFLINE", raising=False)
    require_online("公网接口", endpoint="https://api.example.com/v1")  # 不抛
