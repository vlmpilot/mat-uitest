"""团队授权：本地合同 → 云端身份（attach）、凭证优先级、两个保护。

这一层最容易出的错是**看起来成功**：本地激活打印"已激活 团队版"，团队库却因为
没有 Bearer 而全不可用；或者反过来，标准码续费悄悄把团队授权顶掉。用例按这个口径钉。
"""

from __future__ import annotations

import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from insta_engine import cloudapi, licensing
from insta_engine import team as team_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MAT_HOME", str(h))
    monkeypatch.setenv("MAT_LICENSE_PUBKEY_B64", "unset-by-keypair")  # 由 keypair 覆盖
    return h


@pytest.fixture()
def keypair(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    monkeypatch.setenv("MAT_LICENSE_PUBKEY_B64", pub)
    return pem, pub


def _license(pem, *, days=30, **over):
    data = {"v": 2, "license_id": "LIC-T1", "customer": "测试客户", "plan": "team",
            "features": sorted(licensing.plan_features("team")), "seats": 5,
            "machine_limit": 5, "issued_at": int(time.time()),
            "expires_at": int(time.time()) + days * 86400}
    data.update(over)
    return licensing.sign_license_v2(data, pem)


def _write(home, name, doc):
    p = home / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _v1(plan="pro", days=400):
    body = f"{plan}.{int(time.time()) + days * 86400}.{secrets.token_hex(3)}"
    raw = f"{body}.{licensing._sign(body)}"
    return "MAT-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def _attach_ok(monkeypatch, team_id="lic-LIC-T1"):
    def fake_post(path, payload=None, **kw):
        assert path == "/license/attach"
        assert payload["license"]["v"] == 2
        return {"ok": True, "token": "team-token", "plan": "team", "team_id": team_id,
                "user_id": "lic_LIC-T1_abcdef12", "seats": 5, "customer": "测试客户",
                "license_id": "LIC-T1", "expires_at": int(time.time()) + 86400}

    monkeypatch.setattr(cloudapi, "post", fake_post)


def test_activate_file_attaches_and_bearer_prefers_it(home, keypair, monkeypatch):
    pem, _pub = keypair
    _attach_ok(monkeypatch)
    p = _write(home, "team.json", _license(pem))

    info = licensing.activate_license_file(p)

    assert info["server"]["ok"] is True
    assert info["server"]["team_id"] == "lic-LIC-T1"
    assert licensing.read_server_token() == "team-token"
    # 同一台机器可能先体验过（invite.json 里有一张匿名 token）——团队凭证必须优先
    (home / "invite.json").write_text(json.dumps({"token": "anon"}), encoding="utf-8")
    assert team_mod.bearer() == "team-token"


def test_attach_failure_does_not_block_local_activation(home, keypair, monkeypatch):
    pem, _pub = keypair

    def boom(*a, **k):
        raise cloudapi.CloudError("网络不可达")

    monkeypatch.setattr(cloudapi, "post", boom)
    p = _write(home, "team.json", _license(pem))

    info = licensing.activate_license_file(p)

    assert info["kept_existing"] is False
    assert info["server"]["ok"] is False
    assert licensing.read_license()["plan"] == "team", "本地授权必须已经生效"


def test_shorter_file_keeps_existing(home, keypair, monkeypatch):
    pem, _pub = keypair
    _attach_ok(monkeypatch)
    long_doc = _license(pem, days=365)
    licensing.activate_license_file(_write(home, "long.json", long_doc))
    expires = licensing.read_license()["expires_at"]

    info = licensing.activate_license_file(_write(home, "short.json", _license(pem, days=10)))

    assert info["kept_existing"] is True
    assert licensing.read_license()["expires_at"] == expires


def test_force_allows_shorter_file(home, keypair, monkeypatch):
    pem, _pub = keypair
    _attach_ok(monkeypatch)
    licensing.activate_license_file(_write(home, "long.json", _license(pem, days=365)))

    info = licensing.activate_license_file(
        _write(home, "short.json", _license(pem, days=10)), force=True)

    assert info["kept_existing"] is False
    assert licensing.read_license()["expires_at"] < time.time() + 30 * 86400


def test_standard_code_cannot_downgrade_team_license(home, keypair, monkeypatch):
    pem, _pub = keypair
    _attach_ok(monkeypatch)
    licensing.activate_license_file(_write(home, "team.json", _license(pem)))

    with pytest.raises(ValueError, match="降级"):
        licensing.activate(_v1("pro", 400))

    assert licensing.read_license()["plan"] == "team", "被拒时不能有任何写盘"

    out = licensing.activate(_v1("pro", 400), force=True)
    assert out["plan"] == "pro"
    assert licensing.read_license()["plan"] == "pro"


def test_status_shows_team_and_library(home, keypair, monkeypatch, capsys):
    pem, _pub = keypair
    _attach_ok(monkeypatch)
    licensing.activate_license_file(_write(home, "team.json", _license(pem)))

    def fake_get(path, **kw):
        assert kw.get("bearer") == "team-token"
        if path == "/usage":
            return {"month": "2026-11", "plan": "team", "team_id": "lic-LIC-T1",
                    "gen": {"used": 3, "limit": 10000}}
        return {"cases": [{"app": "com.demo", "name": "登录冒烟", "actor": "alice",
                           "updated_at": "2026-11-01T09:00:00"}]}

    monkeypatch.setattr(cloudapi, "get", fake_get)

    rc = team_mod.status()
    out = capsys.readouterr().out

    assert rc == 0
    assert "lic-LIC-T1" in out            # 和谁一个库
    assert "1 条用例" in out              # 库里有什么
    assert "登录冒烟" in out


def test_status_without_token_tells_how_to_fix(home, keypair, monkeypatch, capsys):
    pem, _pub = keypair
    _attach_ok(monkeypatch)
    licensing.activate_license_file(_write(home, "team.json", _license(pem)))
    (home / "license_server.json").unlink()  # 模拟凭证丢失

    rc = team_mod.status()
    out = capsys.readouterr().out

    assert rc == 1
    assert "mat activate --file" in out


def test_expired_license_does_not_use_team_token(home, keypair):
    """授权过期后，本机不再把「授权换来的凭证」当身份。

    凭证在服务端仍有效（按身份签、不随授权过期而失效），所以真正的闸是服务端的
    `expires_at` 401；这里钉的是**本机不装作用**——否则用户看到"到期了还在跑"，
    等到服务端报错时已经不知道是哪一步的问题。
    """
    pem, _pub = keypair
    (home / "license.json").write_text(
        json.dumps(_license(pem, days=-1), ensure_ascii=False), encoding="utf-8")
    (home / "license_server.json").write_text(
        json.dumps({"token": "team-token", "team_id": "lic-LIC-T1"}), encoding="utf-8")

    assert licensing.read_license()["plan"] == "free"
    assert team_mod.bearer() == "", "授权过期后不该继续用授权凭证"


def test_forced_downgrade_revokes_team_token(home, keypair, monkeypatch):
    """强制降级到标准版：旧团队凭证必须**一起作废**。

    回归锁（2026-11-01 复核）：降级只改了 `license.json`，`license_server.json` 里的
    team token 还在，`bearer()` 继续用它 ⇒ 服务端照旧按 team 档给额度（10,000 次/月），
    而 `mat team status` 还在说「服务端会拒绝」。等于**降级没生效**。
    """
    pem, _pub = keypair
    _attach_ok(monkeypatch)
    licensing.activate_license_file(_write(home, "team.json", _license(pem)))
    assert licensing.read_server_token() == "team-token"

    out = licensing.activate(_v1(plan="pro", days=500), force=True)

    assert out["downgraded"] is True and out["cleared_server_token"] is True
    assert licensing.read_server_token() == "", "降级后旧凭证不该还能被 bearer 取到"
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    assert team_mod.bearer() == ""


def test_team_status_tells_truth_about_leftover_token(home, keypair, monkeypatch, capsys):
    """档位已不含团队协作、本机却还留着凭证 ⇒ 不能说「服务端会拒绝」（它不会）。

    这张凭证可能来自历史残留或手工放置。说反了，人会以为"已经清干净了"。
    """
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    licensing.activate(_v1(plan="pro", days=200))
    licensing.save_server_token("stale-team-token", {"plan": "team"})

    team_mod.status()

    out = capsys.readouterr().out
    assert "仍留有云端凭证" in out
    assert "服务端会拒绝" not in out


def test_cli_warns_when_downgrade_revokes_team_token(home, keypair, monkeypatch, capsys):
    """降级会顺手作废团队凭证——CLI 必须**说出来**。

    不说的话，用户要到下一次 `mat team pull` 才发现库没了，而且会以为是网络问题。
    """
    import argparse

    from insta_engine import __main__ as cli

    pem, _pub = keypair
    _attach_ok(monkeypatch)
    licensing.activate_license_file(_write(home, "team.json", _license(pem)))
    assert licensing.read_server_token() == "team-token"
    monkeypatch.setattr(licensing, "activate_on_server",
                        lambda code: {"token": "new-token", "machines": 1, "max_machines": 2})

    cli.cmd_activate(argparse.Namespace(code=_v1("pro", 500), force=True, file=None,
                                        machines=False, unbind=None))

    out = capsys.readouterr().out
    assert "团队协作能力已失效" in out
    assert licensing.read_server_token() == "", "降级后不该还留着团队凭证"
