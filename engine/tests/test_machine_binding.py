"""Pro 激活登记与机器绑定（T17）客户端侧 —— / §6。

🔴 最要紧的一条：**服务端登记失败绝不阻断本地激活**。
本地 `license.json` 是离线验签写盘的，BYOK 用户不该因为断网就用不了；
服务端那一步换来的是**服务端额度**（300 次/月真实计量）与机器绑定。
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
    # （实测：`test_v1.py::test_vision_fallback_is_opt_in` 就是这么被带红的）。
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class Recorder:
    def __init__(self, response=None):
        self.calls: list[dict] = []
        self.response = response or FakeResponse()

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers or {}})
        return self.response


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
    """🔴 缓存的意义：派生源（网卡 / 主机名）变了，**已绑定的机器不能跟着变**。

    否则换网卡 / 容器重建后 `machine_id` 变新 ⇒ 用户会瞬间用光名额被锁在外面。
    """
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


# -- mat activate ------------------------------------------------------------


def test_activate_registers_on_server_and_prints_the_env_hint(monkeypatch, capsys):
    rec = Recorder(FakeResponse(payload={"ok": True, "plan": "pro", "expires_at": 2000000000,
                                         "machines": 1, "max_machines": 2, "reused": False,
                                         "token": "srv-tok"}))
    monkeypatch.setattr("requests.request", rec)

    assert _run(["activate", _code()]) == 0

    out = capsys.readouterr().out
    assert "已激活 pro" in out
    assert "1/2 台机器" in out
    assert "GEN_API_KEY=srv-tok" in out

    sent = rec.calls[0]["json"]
    assert set(sent) == {"code", "machine_id"}
    assert sent["machine_id"] == licensing.machine_id()
    assert licensing.read_server_token() == "srv-tok"


def test_local_activation_succeeds_when_the_server_is_unreachable(monkeypatch, capsys):
    """🔴 服务端不通**不阻断**本地激活 —— 断网不该让 BYOK 用户用不了。

    只是拿不到服务端额度，且必须**明说**这一点（不能让用户以为已经开通了）。
    """
    def boom(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr("requests.request", boom)

    assert _run(["activate", _code()]) == 0

    out = capsys.readouterr().out
    assert "已激活 pro" in out, "本地激活必须成功"
    assert licensing.read_license()["plan"] == "pro"
    assert "服务端登记未完成" in out
    assert "本地授权已生效" in out
    assert licensing.read_server_token() == "", "没登记成功就不该存凭证"


def test_activate_rejects_a_bad_code_without_any_network(monkeypatch, capsys):
    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)

    assert _run(["activate", "MAT-not-a-real-code"]) == 1

    assert rec.calls == [], "本地就验不过的码，不该往服务端发"
    assert "❌" in capsys.readouterr().out


def test_activate_reports_the_machine_limit_rejection(monkeypatch, capsys):
    """超限是 **409 软拒**：本地照旧激活成功，但要说清"服务端额度没开通"和怎么办。"""
    rec = Recorder(FakeResponse(status_code=409,
                                payload={"detail": "该激活码已绑定 2 台机器（上限 2 台）。"}))
    monkeypatch.setattr("requests.request", rec)

    assert _run(["activate", _code()]) == 0

    out = capsys.readouterr().out
    assert "已激活 pro" in out
    assert "409" in out and "已绑定 2 台机器" in out
    assert licensing.read_license()["plan"] == "pro"


def test_activate_requires_a_code_when_not_managing(monkeypatch, capsys):
    assert _run(["activate"]) == 1
    assert "用法" in capsys.readouterr().err


# -- 重复激活 / 失败原因（2026-10-13）----------------------------------------
#
# 起因：这三种处境原先被打成**同一句**「激活码无效或已过期」或**同一个 "✅ 已激活"**，
# 而它们的下一步动作完全不同 —— 重抄一遍 / 换新码 / 什么都不用做。用户拿着同一句话无从下手。


def test_activating_the_same_code_twice_says_already_activated(monkeypatch, capsys):
    """同一张码在本机再来一次：**不是**"又激活了一次"，要如实说"你已经用过了、没改动"。

    打 "✅ 已激活" 会让用户以为自己又续上了一次（这正是要修的表达缺陷）。
    """
    monkeypatch.setattr("requests.request",
                        Recorder(FakeResponse(payload={"ok": True, "plan": "pro",
                                                       "expires_at": 2000000000, "machines": 1,
                                                       "max_machines": 2, "reused": True,
                                                       "token": "srv-tok"})))
    code = _code()

    assert _run(["activate", code]) == 0
    capsys.readouterr()                                  # 丢掉第一次的输出

    assert _run(["activate", code]) == 0
    out = capsys.readouterr().out
    assert "已经用这张激活码激活过本机了" in out
    assert "未做任何改动" in out
    assert "✅ 已激活" not in out, "重复激活不得再说一遍「已激活」（会让人以为又续上了）"


def test_activating_a_shorter_code_says_it_kept_the_longer_one(monkeypatch, capsys):
    """更短的码进来：**一个字节都没改**，要说清是"保留了更长的"，而不是"已激活"。"""
    monkeypatch.setattr("requests.request",
                        Recorder(FakeResponse(payload={"ok": True, "plan": "pro",
                                                       "expires_at": 2000000000, "machines": 1,
                                                       "max_machines": 2, "reused": True,
                                                       "token": "srv-tok"})))
    now = int(time.time())

    assert _run(["activate", _code(expires=now + 86400 * 365)]) == 0
    capsys.readouterr()

    assert _run(["activate", _code(expires=now + 86400 * 7)]) == 0
    out = capsys.readouterr().out
    assert "已保留更长的那个" in out
    assert "✅ 已激活" not in out


def test_a_second_code_with_the_same_expiry_says_nothing_changed(monkeypatch, capsys):
    """🔴 活动码用户的**高频真实处境**：两枚**不同**的码、**同一个**截止日（都 `--until` 同一天）。

    第二枚写不进去（谁长用谁，相等也不覆盖）——但此时**不能说**"已保留更长的那个"：
    两枚一样长。也不能说"你已经用这张码激活过了"（不是同一张）。得如实说
    "本机已有同等有效期的授权，这张码不必再用"。
    """
    monkeypatch.setattr("requests.request",
                        Recorder(FakeResponse(payload={"ok": True, "plan": "pro",
                                                       "expires_at": 2000000000, "machines": 1,
                                                       "max_machines": 2, "reused": True,
                                                       "token": "srv-tok"})))
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
    """过期 ≠ 抄错了：过期必须把**是哪天过期**说清楚，并与"签名不通过"分开。

    原先两者都只得到「激活码无效或已过期」—— 用户既不知道自己抄错没抄错，
    也不知道这码是"坏了"还是"到期了"。
    """
    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    past = int(time.time()) - 86400 * 30

    assert _run(["activate", _code(expires=past)]) == 1

    out = capsys.readouterr().out
    assert "已于" in out and "过期" in out
    assert time.strftime("%Y-%m-%d", time.localtime(past)) in out
    assert "签名" not in out, "过期不是签名问题，别把两种原因混在一句里"
    assert rec.calls == [], "本地就验不过的码不该往服务端发"


def test_broken_code_tells_the_user_to_copy_the_whole_thing(monkeypatch, capsys):
    """签名对不上 ⇒ 动作是"整段重新复制"（多数是抄漏/截断），不是"码过期了"。"""
    monkeypatch.setattr("requests.request", Recorder())
    code = _code()
    last = "0" if code[-1] != "0" else "1"

    assert _run(["activate", code[:-1] + last]) == 1

    out = capsys.readouterr().out
    assert "签名" in out and "整段" in out
    assert "过期" not in out


def test_server_rejection_does_not_tell_the_user_to_retry(monkeypatch, capsys):
    """🔴 回归门禁：服务端**明确拒绝**（409 超限）时，不得再劝用户"之后重跑即可"。

    重跑一百次结果都一样 —— 那句话说错了方向，用户会陷在重试里。只有**网络没通**
    才值得说"联网后重跑"。
    """
    monkeypatch.setattr("requests.request",
                        Recorder(FakeResponse(status_code=409,
                                              payload={"detail": "该激活码已绑定 2 台机器（上限 2 台）。"})))

    assert _run(["activate", _code()]) == 0

    out = capsys.readouterr().out
    assert "重跑这条命令结果不变" in out
    assert "该激活码已绑定 2 台机器" in out
    assert "之后重跑 `mat activate <激活码>` 即可" not in out


def test_cloud_error_carries_the_http_status(monkeypatch):
    """`CloudError.status` 必须是**结构化字段**：调用方不能靠 `"409" in str(exc)` 猜
    —— 那种写法一改文案就静默失效（本次修的就是被它藏起来的错文案）。"""
    from insta_engine import cloudapi

    monkeypatch.setattr("requests.request",
                        Recorder(FakeResponse(status_code=409, payload={"detail": "x"})))
    with pytest.raises(cloudapi.CloudError) as exc:
        cloudapi.post("/license/activate", {"code": "x"})
    assert exc.value.status == 409


def test_cloud_error_status_is_none_when_the_network_is_down(monkeypatch):
    """网络层失败没有状态码 ⇒ `status is None`，调用方据此说"联网后重跑"。"""
    from insta_engine import cloudapi

    def boom(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr("requests.request", boom)
    with pytest.raises(cloudapi.CloudError) as exc:
        cloudapi.post("/license/activate", {"code": "x"})
    assert exc.value.status is None


# -- --machines / --unbind ---------------------------------------------------


def test_machines_lists_and_marks_this_machine(monkeypatch, capsys):
    licensing.save_server_token("tok", {"machines": 2, "max_machines": 2})
    mine = licensing.machine_id()
    rec = Recorder(FakeResponse(payload={
        "code_nonce": "abc123", "self": mine, "max_machines": 2,
        "machines": [{"machine_id": mine, "created_at": "2026-10-12T00:00:00+00:00"},
                     {"machine_id": "b" * 64, "created_at": "2026-10-11T00:00:00+00:00"}]}))
    monkeypatch.setattr("requests.request", rec)

    assert _run(["activate", "--machines"]) == 0

    out = capsys.readouterr().out
    assert "已绑定 2/2 台机器" in out
    assert "← 本机" in out
    assert rec.calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_unbind_calls_the_server_with_the_token(monkeypatch, capsys):
    licensing.save_server_token("tok", {})
    rec = Recorder(FakeResponse(payload={"ok": True, "unbound": "c" * 64}))
    monkeypatch.setattr("requests.request", rec)

    assert _run(["activate", "--unbind", "c" * 64]) == 0

    assert rec.calls[0]["url"].endswith("/license/unbind")
    assert rec.calls[0]["json"] == {"machine_id": "c" * 64}
    assert "已解绑" in capsys.readouterr().out


def test_unbind_surfaces_server_errors(monkeypatch, capsys):
    licensing.save_server_token("tok", {})
    monkeypatch.setattr("requests.request",
                        Recorder(FakeResponse(status_code=401, payload={"detail": "凭证无效"})))

    assert _run(["activate", "--unbind", "c" * 64]) == 1

    assert "凭证无效" in capsys.readouterr().err


# -- mat status --------------------------------------------------------------


def test_status_shows_server_authorization_and_stays_offline(monkeypatch, tmp_path, capsys):
    from insta_engine import __main__ as cli

    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    licensing.save_server_token("tok", {"machines": 1, "max_machines": 2})

    cli.cmd_status(None)

    out = capsys.readouterr().out
    assert "Pro 服务端授权" in out and "1/2 台机器" in out
    assert rec.calls == [], "mat status 绝不许联网"


def test_status_hides_the_line_before_server_registration(monkeypatch, tmp_path, capsys):
    from insta_engine import __main__ as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("requests.request", Recorder())

    cli.cmd_status(None)

    assert "Pro 服务端授权" not in capsys.readouterr().out


def test_server_token_file_is_private():
    licensing.save_server_token("tok", {"plan": "pro"})
    path = licensing.server_token_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "tok"


def test_cloudapi_is_not_imported_by_other_cli_modules():
    """与邀请同一条纪律：**顶层不许导入**网络客户端 —— 否则任何命令都可能出网。"""
    import subprocess

    code = ("import sys, insta_engine.__main__; print('insta_engine.cloudapi' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd="/")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "导入 CLI 时就把网络客户端拉进来了"
