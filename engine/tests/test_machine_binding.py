"""本机标识与早期激活码的**离线**激活（2026-11-02 起不再服务端登记/绑定机器）。

🔴 两条纪律：
- 早期激活码只做本地验签，**一个网络请求都不发**；
- `~/.mat/machine_id` 仍要稳定缓存（团队授权 `/license/attach` 用它归属云端用量）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import stat
import sys
import time

import pytest

from insta_engine import licensing


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_HOME", str(tmp_path / "mat-home"))
    monkeypatch.delenv("INSTA_HOME", raising=False)
    monkeypatch.delenv("MAT_CLOUD_URL", raising=False)
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    # 🔴 **必须 chdir 到临时目录**：`cli.main()` 会把 **cwd 的 `.env`** 载入 `os.environ`
    # 且不还原（CLI 进程本来就会退出，不算 bug）。在仓库根跑的话，仓库 `.env` 里的
    # `GEN_VL_FALLBACK=1` 之类会**泄漏给后面所有测试** —— 表现为"单独跑绿、全量跑红"的假 flake
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers or {}})
        raise AssertionError(f"早期激活码不该出网：{url}")


def _code(expires: int | None = None, key: str = "mat-beta-public-2026",
          nonce: str = "abc123") -> str:
    """造一张测试码。`nonce` 可换 —— 用来造"**两枚不同的**码"（活动码场景）。"""
    body = f"pro.{expires or int(time.time()) + 86400 * 30}.{nonce}"
    raw = f"{body}." + hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
    return "MAT-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def _run(argv: list[str]) -> int:
    from insta_engine import __main__ as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code or 0


# -- machine_id --------------------------------------------------------------


def test_machine_id_is_a_cached_hash():
    mid = licensing.machine_id()

    assert len(mid) == 64 and int(mid, 16) >= 0, "必须是 64 位十六进制哈希"
    assert licensing.machine_id() == mid, "第二次必须复用缓存"
    assert stat.S_IMODE((licensing.insta_home() / "machine_id").stat().st_mode) == 0o600


def test_machine_id_survives_hardware_changes_once_cached(monkeypatch):
    """🔴 缓存的意义：派生源（网卡 / 主机名）变了，**已登记的机器不能跟着变**。"""
    import uuid as _uuid

    mid = licensing.machine_id()
    monkeypatch.setattr(_uuid, "getnode", lambda: 0xDEADBEEF)
    monkeypatch.setattr("socket.gethostname", lambda: "another-host")

    assert licensing.machine_id() == mid


def test_machine_id_regenerates_if_the_cache_is_corrupt():
    (licensing.insta_home() / "machine_id").parent.mkdir(parents=True, exist_ok=True)
    (licensing.insta_home() / "machine_id").write_text("not-a-hash", encoding="utf-8")

    mid = licensing.machine_id()

    assert len(mid) == 64


def test_machine_id_is_not_the_raw_hardware_string():
    """上传的必须是哈希（原始硬件信息不出本机）。"""
    import platform
    import socket
    import uuid as _uuid

    raw = "|".join([f"{_uuid.getnode():012x}", platform.system(),
                    platform.machine(), socket.gethostname()])
    assert licensing.machine_id() != raw
    assert raw not in licensing.machine_id()


# -- mat activate（纯离线）----------------------------------------------------


def test_activate_is_purely_offline(monkeypatch, capsys):
    """2026-11-02：激活码不再向服务端登记 —— 一个请求都不发。"""
    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)

    assert _run(["activate", _code()]) == 0

    out = capsys.readouterr().out
    assert "已激活 单人版" in out
    assert rec.calls == [], "早期码激活不该出网"
    assert licensing.read_license()["plan"] == "pro"
    assert licensing.read_server_token() == "", "不再由激活码领取服务端凭证"


def test_activate_rejects_a_bad_code_without_any_network(monkeypatch, capsys):
    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)

    assert _run(["activate", "MAT-not-a-real-code"]) == 1

    assert rec.calls == []
    assert "❌" in capsys.readouterr().out


def test_activate_requires_a_code(monkeypatch, capsys):
    assert _run(["activate"]) == 1
    assert "用法" in capsys.readouterr().err


# -- 重复激活 / 失败原因（2026-10-13）----------------------------------------
#
# 起因：这三种处境原先被打成**同一句**「激活码无效或已过期」或**同一个 "✅ 已激活"**，
# 而它们的下一步动作完全不同 —— 重抄一遍 / 换新码 / 什么都不用做。用户拿着同一句话无从下手。


def test_activating_the_same_code_twice_says_already_activated(capsys):
    """同一张码在本机再来一次：**不是**"又激活了一次"，要如实说"你已经用过了、没改动"。"""
    code = _code()

    assert _run(["activate", code]) == 0
    capsys.readouterr()                                  # 丢掉第一次的输出

    assert _run(["activate", code]) == 0
    out = capsys.readouterr().out
    assert "已经用这张激活码激活过本机了" in out
    assert "未做任何改动" in out
    assert "✅ 已激活" not in out, "重复激活不得再说一遍「已激活」（会让人以为又续上了）"


def test_activating_a_shorter_code_says_it_kept_the_longer_one(capsys):
    """更短的码进来：**一个字节都没改**，要说清是"保留了更长的"，而不是"已激活"。"""
    now = int(time.time())

    assert _run(["activate", _code(expires=now + 86400 * 365)]) == 0
    capsys.readouterr()

    assert _run(["activate", _code(expires=now + 86400 * 7)]) == 0
    out = capsys.readouterr().out
    assert "已保留更长的那个" in out
    assert "✅ 已激活" not in out


def test_a_second_code_with_the_same_expiry_says_nothing_changed(capsys):
    """🔴 活动码用户的**高频真实处境**：两枚**不同**的码、**同一个**截止日。

    第二枚写不进去（谁长用谁，相等也不覆盖）——但此时**不能说**"已保留更长的那个"：
    两枚一样长。也不能说"你已经用这张码激活过了"（不是同一张）。得如实说
    "本机已有同等有效期的授权，这张码不必再用"。
    """
    same_day = int(time.time()) + 86400 * 90

    assert _run(["activate", _code(expires=same_day, nonce="aaaaaa")]) == 0
    capsys.readouterr()

    assert _run(["activate", _code(expires=same_day, nonce="bbbbbb")]) == 0
    out = capsys.readouterr().out
    assert "已有同等有效期的授权" in out and "这张码不必再用" in out
    assert "更长的那个" not in out, "两枚码一样长，不能说「保留更长的」"
    assert "已经用这张激活码激活过" not in out, "不是同一张码，别混为一谈"
    assert "✅ 已激活" not in out


def test_expired_code_says_the_exact_date_and_stays_offline(monkeypatch, capsys):
    """过期 ≠ 抄错了：过期必须把**是哪天过期**说清楚，并与"签名不通过"分开。"""
    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    past = int(time.time()) - 86400 * 30

    assert _run(["activate", _code(expires=past)]) == 1

    out = capsys.readouterr().out
    assert "已于" in out and "过期" in out
    assert time.strftime("%Y-%m-%d", time.localtime(past)) in out
    assert "签名" not in out, "过期不是签名问题，别把两种原因混在一句里"
    assert rec.calls == []


def test_broken_code_tells_the_user_to_copy_the_whole_thing(monkeypatch, capsys):
    """签名对不上 ⇒ 动作是"整段重新复制"（多数是抄漏/截断），不是"码过期了"。"""
    monkeypatch.setattr("requests.request", Recorder())
    code = _code()
    last = "0" if code[-1] != "0" else "1"

    assert _run(["activate", code[:-1] + last]) == 1

    out = capsys.readouterr().out
    assert "签名" in out and "整段" in out
    assert "过期" not in out


# -- mat status --------------------------------------------------------------


def test_status_shows_server_authorization_and_stays_offline(monkeypatch, tmp_path, capsys):
    from insta_engine import __main__ as cli

    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    licensing.save_server_token("tok", {"plan": "team", "team_id": "lic_x"})

    cli.cmd_status(None)

    out = capsys.readouterr().out
    assert "服务端授权（团队版）" in out
    assert rec.calls == [], "mat status 绝不许联网"


def test_status_hides_the_line_before_team_attach(monkeypatch, tmp_path, capsys):
    from insta_engine import __main__ as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("requests.request", Recorder())

    cli.cmd_status(None)

    assert "服务端授权" not in capsys.readouterr().out


def test_server_token_file_is_private():
    licensing.save_server_token("tok", {"plan": "pro"})
    path = licensing.server_token_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "tok"


def test_cloudapi_is_not_imported_by_other_cli_modules():
    """底层纪律：**顶层不许导入**网络客户端 —— 否则任何命令都可能出网。"""
    import subprocess

    code = ("import sys, insta_engine.__main__; print('insta_engine.cloudapi' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd="/")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "导入 CLI 时就把网络客户端拉进来了"


def test_status_server_line_uses_the_token_plan(monkeypatch, tmp_path, capsys):
    """团队凭证不能显示成「服务端授权（单人版）」——同一屏两个档位自相矛盾。"""
    from insta_engine import __main__ as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("requests.request", Recorder())
    licensing.save_server_token("tok", {"plan": "team", "team_id": "lic_x"})

    cli.cmd_status(None)

    out = capsys.readouterr().out
    assert "服务端授权（团队版）" in out
    assert "服务端授权（单人版）" not in out


def _fake_usage(plan: str) -> dict:
    return {"month": "2026-11", "plan": plan, "gen_count": 0, "vision_count": 0,
            "gen_byok": 0, "vision_byok": 0, "gen_official": 0, "vision_official": 0,
            "gen_limit": 10000, "gen_enforced": False, "vision_limit": 1000}


def test_status_mode_follows_license_not_the_token(monkeypatch, tmp_path, capsys):
    """「使用方式」只由**授权**决定：官方代理下不能因为 .env 里是 free key 就显示体验模式。"""
    from insta_engine import __main__ as cli
    from insta_engine import promptstore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("requests.request", Recorder())
    monkeypatch.setattr(promptstore, "is_server", lambda: True)
    monkeypatch.setattr(cli, "_server_usage", lambda: {
        "month": "2026-11", "plan": "free",
        "gen": {"used": 0, "limit": 60}, "vision": {"used": 0, "limit": 20}})
    monkeypatch.setattr(cli.UsageStore, "snapshot", lambda self: _fake_usage("team"))

    cli.cmd_status(None)

    out = capsys.readouterr().out
    assert "团队模式" in out, out
    assert "单人模式" not in out, "不能因为 .env 里是 free 凭证就把团队授权显示成单人"


def test_status_mode_falls_back_to_token_when_no_license(monkeypatch, tmp_path, capsys):
    """没有本地授权（free）时仍按凭证的 plan 显示——纯 token 用户不该被显示成单人档。"""
    from insta_engine import __main__ as cli
    from insta_engine import promptstore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("requests.request", Recorder())
    monkeypatch.setattr(promptstore, "is_server", lambda: True)
    monkeypatch.setattr(cli, "_server_usage", lambda: {
        "month": "2026-11", "plan": "team",
        "gen": {"used": 0, "limit": 10000}, "vision": {"used": 0, "limit": 1000}})
    monkeypatch.setattr(cli.UsageStore, "snapshot", lambda self: _fake_usage("free"))

    cli.cmd_status(None)

    assert "团队模式" in capsys.readouterr().out


def test_status_warns_when_token_plan_differs_from_license(monkeypatch, tmp_path, capsys):
    """授权是团队、凭证还是 free ⇒ 额度按凭证算，必须说出来。"""
    from insta_engine import __main__ as cli
    from insta_engine import promptstore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("requests.request", Recorder())
    monkeypatch.setattr(promptstore, "is_server", lambda: True)
    monkeypatch.setattr(cli, "_server_usage", lambda: {
        "month": "2026-11", "plan": "free",
        "gen": {"used": 0, "limit": 60}, "vision": {"used": 0, "limit": 20}})
    monkeypatch.setattr(cli.UsageStore, "snapshot", lambda self: _fake_usage("team"))

    cli.cmd_status(None)

    out = capsys.readouterr().out
    assert "凭证档位（free）与本机授权（team）不一致" in out
    assert "GEN_API_KEY" in out
