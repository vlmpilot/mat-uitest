"""客户端验 `MAT2-` 码：只持公钥，签不了；旧 `MAT-` 码继续可用。"""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from insta_engine import licensing

FUTURE = int(time.time()) + 30 * 86400


def _sign(pem: str, body: str) -> str:
    key = serialization.load_pem_private_key(pem.encode(), None)
    sig = key.sign(body.encode())
    return "MAT2-" + body + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


@pytest.fixture()
def keypair(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    pub = base64.b64encode(key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)).decode()
    monkeypatch.setenv("MAT_CODE_PUBKEY_B64", pub)
    return pem


def test_mat2_verifies(keypair):
    code = _sign(keypair, f"pro.{FUTURE}.abc123")
    info = licensing.verify_code(code)
    assert info == {"plan": "pro", "expires_at": FUTURE, "months": 0}


def test_mat2_tampered_is_signature_error(keypair):
    code = _sign(keypair, f"pro.{FUTURE}.abc123")
    tampered = code.replace("abc123", "abc124")
    info, reason = licensing.verify_code_detailed(tampered)
    assert info is None and reason == licensing.REASON_SIGNATURE


def test_mat2_expired(keypair):
    code = _sign(keypair, f"pro.{int(time.time()) - 10}.abc123")
    assert licensing.verify_code(code) is None


def test_mat2_signed_by_other_key_rejected(keypair):
    """别人自签的 MAT2 码：公钥对不上，必须拒（这正是 v2 的意义）。"""
    other = Ed25519PrivateKey.generate()
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    code = _sign(other_pem, f"pro.{FUTURE}.abc123")
    info, reason = licensing.verify_code_detailed(code)
    assert info is None and reason == licensing.REASON_SIGNATURE


def test_legacy_v1_code_still_verifies():
    """v1（HMAC）码继续可用：老用户手里的码不能因为升级就作废。"""
    body = f"pro.{FUTURE}.abc123"
    legacy = "MAT-" + body + "." + licensing._sign(body)
    assert licensing.verify_code(legacy)["plan"] == "pro"
