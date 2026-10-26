"""P0-3：license v2（Ed25519）验签/篡改/过期/降级 + activate_license_file + v1 兼容。"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from insta_engine import licensing


@pytest.fixture()
def keypair(monkeypatch):
    """每测试独立密钥对；公钥经 env 注入验签端（生产同机制）。"""
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    monkeypatch.setenv("MAT_LICENSE_PUBKEY_B64", pub_b64)
    return pem


def _lic(**over):
    data = {"v": 2, "license_id": "LIC-TEST-1", "customer": "测试客户",
            "plan": "private", "features": sorted(licensing.FEATURE_PRIVATE),
            "seats": 10, "machine_limit": 2,
            "issued_at": int(time.time()), "expires_at": int(time.time()) + 86400}
    data.update(over)
    return data


def test_roundtrip_sign_and_verify(keypair):
    signed = licensing.sign_license_v2(_lic(), keypair)
    info, reason = licensing.verify_license_v2(signed)
    assert reason is None and info["plan"] == "private" and info["seats"] == 10
    assert set(info["features"]) == set(licensing.FEATURE_PRIVATE)


def test_tamper_detected(keypair):
    signed = licensing.sign_license_v2(_lic(), keypair)
    signed["seats"] = 9999  # 改一个字段 → 规范化载荷变化 → 验签必败
    info, reason = licensing.verify_license_v2(signed)
    assert info is None and reason == "signature"


def test_expired(keypair):
    signed = licensing.sign_license_v2(_lic(expires_at=int(time.time()) - 10), keypair)
    info, reason = licensing.verify_license_v2(signed)
    assert info is None and reason == "expired"


def test_wrong_pubkey_rejected(keypair, monkeypatch):
    signed = licensing.sign_license_v2(_lic(), keypair)
    other = Ed25519PrivateKey.generate()
    monkeypatch.setenv("MAT_LICENSE_PUBKEY_B64", base64.b64encode(
        other.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)).decode())
    info, reason = licensing.verify_license_v2(signed)
    assert info is None and reason == "signature"


def test_malformed(keypair):
    info, reason = licensing.verify_license_v2({"v": 2})
    assert info is None and reason == "malformed"
    info, reason = licensing.verify_license_v2({"not": "v2"})
    assert info is None and reason == "malformed"


def test_activate_file_writes_and_read_license_v2(tmp_path, monkeypatch, keypair):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    signed = licensing.sign_license_v2(_lic(), keypair)
    p = tmp_path / "license_v2.json"
    p.write_text(json.dumps(signed, ensure_ascii=False), encoding="utf-8")

    info = licensing.activate_license_file(p)
    assert info["plan"] == "private" and info["customer"] == "测试客户"

    lic = licensing.read_license()
    assert lic["plan"] == "private" and lic["source"] == "v2"
    assert set(lic["features"]) == set(licensing.FEATURE_PRIVATE)
    assert lic["seats"] == 10


def test_v1_code_still_maps_to_core(tmp_path, monkeypatch):
    """v1 Pro 激活码完全兼容：plan=pro → CORE 功能集、source=v1。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_LICENSE_SECRET", "test-only")
    from test_v1 import mint_test_code

    licensing.activate(mint_test_code("pro", months=1))
    lic = licensing.read_license()
    assert lic["plan"] == "pro" and lic["source"] == "v1"
    assert set(lic["features"]) == set(licensing.FEATURE_CORE)


def test_bad_v2_degrades_to_free(tmp_path, monkeypatch, keypair):
    """坏档 fail closed：按免费档运行，但把原因带出去给 status 展示。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    signed = licensing.sign_license_v2(_lic(), keypair)
    signed["seats"] = 1  # 篡改
    p = tmp_path / "license.json"
    p.write_text(json.dumps(signed), encoding="utf-8")

    lic = licensing.read_license()
    assert lic["plan"] == "free" and lic.get("error") == "signature"
