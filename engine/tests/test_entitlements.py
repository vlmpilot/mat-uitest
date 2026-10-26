"""P0-3：权益矩阵与门控（entitlements）。"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from insta_engine import entitlements, licensing
from insta_engine.licensing import FEATURE_CORE, FEATURE_PRIVATE, FEATURE_TEAM


def test_free_has_core_only(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    ent = entitlements.current()
    assert ent.plan == "free" and ent.features == FEATURE_CORE
    assert ent.seats is None and ent.source == "none"


def test_require_raises_for_missing_feature(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    with pytest.raises(entitlements.EntitlementError, match="team_share"):
        entitlements.require("team_share")
    entitlements.require("run")  # 核心能力永不加锁
    entitlements.require("api_assert")


def _issue_v2(tmp_path, monkeypatch, plan: str, features: frozenset, seats: int):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("MAT_LICENSE_PUBKEY_B64", base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)).decode())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    data = {"v": 2, "license_id": f"LIC-{plan}", "customer": "测试客户", "plan": plan,
            "features": sorted(features), "seats": seats, "machine_limit": 2,
            "issued_at": int(time.time()), "expires_at": int(time.time()) + 3600}
    signed = licensing.sign_license_v2(data, pem)
    p = tmp_path / "license.json"
    p.write_text(json.dumps(signed), encoding="utf-8")
    licensing.activate_license_file(p)


def test_v2_team_unlocks_team_features(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    _issue_v2(tmp_path, monkeypatch, "team", FEATURE_TEAM, 5)

    ent = entitlements.current()
    assert ent.plan == "team" and ent.seats == 5 and ent.features == FEATURE_TEAM
    entitlements.require("team_share")
    entitlements.require("report_share")
    entitlements.require("audit_log")
    with pytest.raises(entitlements.EntitlementError, match="private_deploy"):
        entitlements.require("private_deploy")


def test_v2_private_unlocks_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    _issue_v2(tmp_path, monkeypatch, "private", FEATURE_PRIVATE, 10)

    ent = entitlements.current()
    assert ent.plan == "private" and ent.features == FEATURE_PRIVATE
    entitlements.require("private_deploy")
    entitlements.require("offline")
    with pytest.raises(entitlements.EntitlementError):
        entitlements.require("nonexistent_feature")
