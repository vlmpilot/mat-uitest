"""邀请（分享得额度）客户端测试 —— 。

不联网：`requests.request` 全部被替换成假的。锁的都是"会让用户被坑"的地方：
- 密钥**永不上传**（只上传 sha256）；
- 台账读不到时**不许显示 0**（§6.4）；
- **只有 `mat invite*` 会发起注册**，其它命令一律不得静默注册（§6.5 的可验证纪律）。
"""

from __future__ import annotations

import json
import stat
import sys

import pytest

from insta_engine import invite as inv


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """所有用例都在临时 MAT_HOME + 临时 cwd 里跑。

    ⚠️ **chdir 不能省**：`cli.main()` 会把 **cwd 的 `.env`** 载入 `os.environ` 且不还原
    （CLI 进程本来就会退出，不算 bug）。在仓库根跑的话，仓库 `.env` 里的
    `GEN_VL_FALLBACK=1` 之类会**泄漏给后面所有测试** —— 表现为"单独跑绿、全量跑红"的假 flake。
    """
    monkeypatch.setenv("MAT_HOME", str(tmp_path / "mat-home"))
    monkeypatch.delenv("INSTA_HOME", raising=False)
    monkeypatch.delenv("MAT_CLOUD_URL", raising=False)
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        if self.text and not self._payload:
            raise ValueError("not json")
        return self._payload


class Recorder:
    """替换 `requests.request`：记录调用，返回可控响应。"""

    def __init__(self, response: FakeResponse | None = None):
        self.calls: list[dict] = []
        self.response = response or FakeResponse()

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers or {}, "timeout": timeout})
        return self.response


# -- 本地密钥 ---------------------------------------------------------------


def test_client_key_is_generated_persisted_and_private():
    assert not inv.key_path().exists()
    first = inv.ensure_client_key()
    assert len(first) >= 32
    assert inv.ensure_client_key() == first, "第二次必须复用，不许每次重生成（否则身份会漂）"

    mode = stat.S_IMODE(inv.key_path().stat().st_mode)
    assert mode == 0o600, f"密钥文件权限应为 0600，实为 {oct(mode)}"


def test_public_key_is_sha256_of_client_key_and_stable():
    import hashlib

    client_key = inv.ensure_client_key()
    assert inv.public_key() == hashlib.sha256(client_key.encode()).hexdigest()
    assert inv.public_key() == inv.public_key()


def test_public_key_is_not_the_client_key():
    """🔴 上传的必须是**哈希**：密钥本身永不出本机。"""
    client_key = inv.ensure_client_key()
    assert inv.public_key() != client_key
    assert client_key not in inv.public_key()


def test_cloud_url_override(monkeypatch):
    assert inv.cloud_url() == inv.DEFAULT_CLOUD_URL
    monkeypatch.setenv("MAT_CLOUD_URL", "https://self.hosted/")
    assert inv.cloud_url() == "https://self.hosted"


def test_auth_token_prefers_state_then_env(monkeypatch):
    monkeypatch.setenv("GEN_API_KEY", "env-key")
    assert inv.auth_token() == "env-key"
    inv.save_state({"token": "state-key"})
    assert inv.auth_token() == "state-key"


# -- 网络动作 ---------------------------------------------------------------


def test_register_sends_only_public_key_and_saves_state(monkeypatch):
    rec = Recorder(FakeResponse(payload={"user_id": "inv_1", "invite_code": "ABCD2345",
                                         "token": "tok"}))
    monkeypatch.setattr("requests.request", rec)

    state = inv.register()

    sent = rec.calls[0]["json"]
    assert list(sent) == ["public_key"], f"出站内容只该有公钥，实为 {sent}"
    assert sent["public_key"] == inv.public_key()
    assert "secret" not in json.dumps(sent)
    assert state["invite_code"] == "ABCD2345"
    assert inv.load_state()["token"] == "tok"
    assert stat.S_IMODE(inv.state_path().stat().st_mode) == 0o600


def test_accept_sends_code_and_public_key(monkeypatch):
    rec = Recorder(FakeResponse(payload={"ok": True, "status": "pending",
                                         "user_id": "inv_2", "invite_code": "ZZZZ2222",
                                         "token": "tok2"}))
    monkeypatch.setattr("requests.request", rec)

    inv.accept("abcd-2345")

    sent = rec.calls[0]["json"]
    assert set(sent) == {"invite_code", "public_key"}
    assert sent["invite_code"] == "abcd-2345"


def test_qualify_and_ledger_use_bearer_token(monkeypatch):
    inv.save_state({"token": "tok"})
    rec = Recorder(FakeResponse(payload={"qualified": False}))
    monkeypatch.setattr("requests.request", rec)

    inv.qualify()
    inv.ledger()

    assert rec.calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert rec.calls[0]["url"].endswith("/invite/qualify")
    assert rec.calls[1]["method"] == "GET" and rec.calls[1]["url"].endswith("/invite")


def test_network_failure_gives_actionable_message(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no route to host")

    monkeypatch.setattr("requests.request", boom)
    with pytest.raises(inv.InviteError) as exc:
        inv.ledger()
    msg = str(exc.value)
    assert "联网" in msg, "必须告诉用户这是联网问题，而不是丢一个 OSError"
    assert "其余功能不受影响" in msg


def test_server_rejection_surfaces_detail(monkeypatch):
    rec = Recorder(FakeResponse(status_code=429, payload={"detail": "今日注册过于频繁"}))
    monkeypatch.setattr("requests.request", rec)
    with pytest.raises(inv.InviteError) as exc:
        inv.register()
    assert "429" in str(exc.value) and "今日注册过于频繁" in str(exc.value)


# -- 🔴 不静默注册（可验证纪律）------------------------------


def _run(argv: list[str]) -> int:
    """跑 CLI 并返回退出码 —— `main()` 内部会 `sys.exit()`，所以必须接住。"""
    from insta_engine import __main__ as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code or 0


def test_status_never_triggers_invite_network(monkeypatch, tmp_path, capsys):
    """`mat status` 不许碰邀请接口 —— 否则就是"启动时静默注册"从后门回来了。"""
    from insta_engine import __main__ as cli

    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)

    cli.cmd_status(None)

    assert rec.calls == [], f"status 不该发起任何邀请请求，实为 {rec.calls}"
    assert "invite" not in capsys.readouterr().out


def test_importing_cli_does_not_pull_in_the_invite_module():
    """🔴 **顶层不许导入邀请模块** —— 顶层导入等于让每个命令都具备联网能力。

    这是行为断言（跑一个干净解释器看 `sys.modules`），不是文本匹配：
    "源码里没有那行字"和"运行时真的没导入"是两件事（同族 A5）。
    """
    import subprocess

    code = ("import sys, insta_engine.__main__; "
            "print('insta_engine.invite' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd="/")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "导入 CLI 时就把邀请模块拉进来了"


# -- 里程碑通知（§3）--------------------------------------------------------


def test_notify_first_run_does_nothing_without_an_accepted_invite(monkeypatch):
    """🔴 **没接受过邀请时一个请求都不发**。

    一个跑测试的命令不该因为我们想知道什么而悄悄出网 —— 这条护栏就是防"静默联网"的。
    """
    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)

    assert inv.notify_first_run() is None
    assert rec.calls == [], f"没有邀请关系却发了请求：{rec.calls}"


def test_notify_first_run_fires_once_and_only_once(monkeypatch):
    """接受过邀请 ⇒ 跑通首轮时通知一次；之后再跑**不再重复通知**（幂等，别刷服务端）。"""
    rec = Recorder(FakeResponse(payload={"qualified": True}))
    monkeypatch.setattr("requests.request", rec)
    inv.save_state({"user_id": "inv_2", "token": "tok", "accepted": "ABCD2345"})

    assert inv.notify_first_run() is not None
    assert len(rec.calls) == 1 and rec.calls[0]["url"].endswith("/invite/qualify")

    assert inv.notify_first_run() is None
    assert len(rec.calls) == 1, "第二次不该再发（本地已记 qualified_at）"


def test_notify_first_run_swallows_network_errors(monkeypatch):
    """🔴 邀请是旁路功能 —— **跑测试绝不能因为它而失败**。"""
    def boom(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr("requests.request", boom)
    inv.save_state({"user_id": "inv_2", "token": "tok", "accepted": "ABCD2345"})

    assert inv.notify_first_run() is None          # 不抛
    assert inv.load_state().get("qualified_at") is None, "失败不该记成已通知"


def test_run_command_never_qualifies_without_invite(monkeypatch, tmp_path):
    """`mat run` 的收尾钩子在没有邀请关系时也不许出网。"""
    from insta_engine import __main__ as cli

    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)

    cli._notify_invite_after_run()

    assert rec.calls == []


# -- 领奖：不许把有效期改短（§4）--------------------------------------------


def _sign(expires: int, key: str = "mat-beta-public-2026") -> str:
    """签一张能被 `licensing.verify_code` 验过的码（测试专用，与引擎口径一致）。"""
    import hashlib
    import hmac

    body = f"pro.{expires}.abc123"
    raw = f"{body}." + hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
    return "MAT-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def _install_license(code: str) -> None:
    from insta_engine import licensing

    licensing.license_path().parent.mkdir(parents=True, exist_ok=True)
    licensing.license_path().write_text(json.dumps({"code": code}), encoding="utf-8")


def test_claim_pro_reward_does_not_shorten_a_longer_local_license():
    """🔴 服务端签的是「现在 + 累计天数」；用户自己买的年付可能更长。

    直接 `activate()` 会把有效期**改短** —— "好心办坏事"的典型，
    所以只在**比本机更晚**时才激活。
    """
    far_future = 4102444800          # 2100-01-01
    _install_license(_sign(far_future))

    out = inv.claim_pro_reward({"pro_code": {"code": _sign(2000000000),
                                             "expires_at": 2000000000}})

    assert out == {"activated": False, "reason": "本机已有更长的有效期",
                   "expires_at": far_future}


def test_claim_pro_reward_activates_when_it_extends():
    """比本机晚 ⇒ 真的激活（阳性对照，证明上一条不是在测"什么都不做"）。"""
    from insta_engine import licensing

    _install_license(_sign(1900000000))
    future = 2000000000

    out = inv.claim_pro_reward({"pro_code": {"code": _sign(future), "expires_at": future}})

    assert out == {"activated": True, "expires_at": future}
    assert licensing.read_license()["plan"] == "pro"


def test_claim_pro_reward_returns_none_without_a_code():
    assert inv.claim_pro_reward({"pro_code": None}) is None
    assert inv.claim_pro_reward({"pro_code": {"status": "pending_sign"}}) is None


def test_invite_command_is_the_only_one_that_registers(monkeypatch, tmp_path, capsys):
    """阳性对照：`mat invite --yes` **应该**发起注册 —— 证明上一条护栏不是空转的。"""
    rec = Recorder(FakeResponse(payload={"user_id": "inv_1", "invite_code": "ABCD2345",
                                         "token": "tok"}))
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)

    assert _run(["invite", "--yes"]) == 0

    assert any(c["url"].endswith("/invite/register") for c in rec.calls), \
        "mat invite 必须发起注册（否则护栏测的是个不存在的行为）"
    out = capsys.readouterr().out
    assert "ABCD-2345" in out
    assert "匿名 ID" in out, "首次注册必须告知用户（§6.5）"


def test_invite_refuses_silent_registration_when_not_a_tty(monkeypatch, tmp_path, capsys):
    """非交互环境且没给 `--yes` ⇒ **拒绝注册**，而不是默默注册。"""
    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    assert _run(["invite"]) == 1

    assert rec.calls == [], "拒绝之后不许真的发请求"
    assert "--yes" in capsys.readouterr().err


# -- 台账显示纪律（§6.4）----------------------------------------------------


def test_ledger_offline_does_not_print_zero(monkeypatch, tmp_path, capsys):
    """🔴 拉不到台账时**不许显示 0** —— `0` 会被读成「我的邀请全丢了」。"""

    def boom(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr("requests.request", boom)
    monkeypatch.chdir(tmp_path)
    inv.save_state({"user_id": "inv_1", "invite_code": "ABCD2345", "token": "tok"})

    assert _run(["invite"]) == 0

    out = capsys.readouterr().out
    assert "已邀请" not in out, f"读不到台账时不许渲染台账数字，实为：{out}"
    assert "这不等于你没有邀请" in out


def test_ledger_shows_pending_and_capped(monkeypatch, tmp_path, capsys):
    """`待生效` 与 `超额未计酬` 必须显示 —— 折叠掉用户会以为功能坏了。"""
    rec = Recorder(FakeResponse(payload={
        "user_id": "inv_1", "invited": 5, "qualified": 4, "pending": 1,
        "credited_this_month": 4, "monthly_cap": 4, "capped": 0,
        "reward_kind": "server_gen", "reward_amount": 50.0}))
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    inv.save_state({"user_id": "inv_1", "invite_code": "ABCD2345", "token": "tok"})

    assert _run(["invite"]) == 0

    out = capsys.readouterr().out
    assert "待生效" in out and "1 人" in out
    assert "对方还没跑通第一轮" in out
    assert "服务端生成 50 次" in out


def test_status_shows_cached_ledger_and_stays_offline(monkeypatch, tmp_path, capsys):
    """`mat status` 显示**本地快照**，且**绝不联网** —— 哪怕用户已经加入了邀请。

    "只有 `mat invite*` 会出网"是一条可验证的纪律（§6.5）。status 是最高频的命令，
    在这里破一次，纪律就再也说不清了。
    """
    from insta_engine import __main__ as cli

    rec = Recorder()
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    inv.save_state({"invite_code": "ABCD2345", "token": "tok",
                    "ledger_synced_at": "2026-10-11 23:00",
                    "ledger_cache": {"invited": 3, "qualified": 2, "pending": 1,
                                     "credited_this_month": 2, "monthly_cap": 4,
                                     "reward_kind": "server_gen", "reward_amount": 50.0}})

    cli.cmd_status(None)

    out = capsys.readouterr().out
    assert "已邀请 3 人" in out and "待生效 1 人" in out
    assert "ABCD-2345" in out
    assert rec.calls == [], f"mat status 绝不许联网，实为 {rec.calls}"


def test_status_says_not_synced_instead_of_showing_zero(monkeypatch, tmp_path, capsys):
    """有邀请码但没同步过 ⇒ 说"还没同步过"，**不许显示 0**（§6.4）。"""
    from insta_engine import __main__ as cli

    monkeypatch.setattr("requests.request", Recorder())
    monkeypatch.chdir(tmp_path)
    inv.save_state({"invite_code": "ABCD2345", "token": "tok"})

    cli.cmd_status(None)

    out = capsys.readouterr().out
    assert "还没同步过" in out
    assert "已邀请 0" not in out


# -- 凭证的出口只有一个：`mat token`（2026-10-14 发起人定）---------------------
#
# 以前 `mat invite` 会顺带打印 `.env` 三行（含 `GEN_API_KEY`）—— 那让"邀请"和"取凭证"
# 混成一条命令：拿到邀请码的人会以为**那就是凭证**，而"凭证从哪来"这件事也就没了门槛。
# 现在：**`mat invite` 一个字都不提凭证**，只给邀请码；凭证只在 `mat token`（且那条命令
# 刻意不进 `mat --help`，获取方式只在公众号）。这一节把新契约钉住。


def test_invite_never_prints_the_token(monkeypatch, tmp_path, capsys):
    """🔴 `mat invite` 只给「邀请码」，**绝不吐凭证**。

    ⚠️ 只是**不打印**：`invite.json` 里照样存着 token（`inv.register()` 落的盘），
    否则 `mat token` 就无东西可读了 —— 这条也在下面断言了。
    """
    rec = Recorder(FakeResponse(payload={"user_id": "inv_1", "invite_code": "ABCD2345",
                                         "token": "tok-abc"}))
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)

    _run(["invite", "--yes"])

    out = capsys.readouterr().out
    assert "tok-abc" not in out, "mat invite 泄露了凭证"
    assert "GEN_API_KEY" not in out, "凭证片段不该在邀请命令里出现"
    assert "GEN_PROMPT_MODE" not in out
    assert "ABCD-2345" in out, "邀请码仍要正常打印"
    assert "公众号" in out, "要留一句**不点名命令**的指路，否则跑过 invite 的人找不到凭证入口"
    assert inv.load_state().get("token") == "tok-abc", "凭证仍要落盘，只是不打印"


def test_invite_pointer_is_suppressed_when_already_on_the_official_proxy(
        monkeypatch, tmp_path, capsys):
    """已经在用官方代理了就别再指路（与凭证提示用**同一个**抑制条件）。"""
    rec = Recorder(FakeResponse(payload={"user_id": "inv_1", "invite_code": "ABCD2345",
                                         "token": "tok-abc"}))
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")

    _run(["invite", "--yes"])

    assert "公众号" not in capsys.readouterr().out


def test_accept_never_prints_the_token(monkeypatch, tmp_path, capsys):
    """被邀请人同样**不**在邀请命令里拿到凭证（与 `mat invite` 同一口径）。"""
    rec = Recorder(FakeResponse(payload={"ok": True, "status": "pending", "note": "已登记",
                                         "user_id": "inv_2", "invite_code": "ZZZZ2222",
                                         "token": "tok-bee"}))
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)

    _run(["invite", "--accept", "ABCD2345"])

    out = capsys.readouterr().out
    assert "tok-bee" not in out
    assert "GEN_API_KEY" not in out


def test_invite_never_edits_the_users_env_file(monkeypatch, tmp_path):
    """🔴 `mat invite` **不许动用户的 `.env`** —— 改配置是显式动作。

    "顺手帮用户改配置"是这类功能最容易犯的错：看起来体贴，实际是在用户没同意的情况下
    改动他的工作区（同族：不许静默注册）。凭证从邀请命令里移走之后这条更容易守，
    但**测试要留着** —— 将来有人"顺手加个写 env 的便利"时，撞上它的应该是这里。
    """
    rec = Recorder(FakeResponse(payload={"user_id": "inv_1", "invite_code": "ABCD2345",
                                         "token": "tok-abc"}))
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=1\n", encoding="utf-8")

    _run(["invite", "--yes"])

    assert env_file.read_text(encoding="utf-8") == "KEEP=1\n", "用户的 .env 被改了"


def test_token_command_prints_token_on_stdout_and_hint_on_stderr(monkeypatch, tmp_path, capsys):
    """凭证的唯一出口 `mat token`：**stdout 只许有 token**，用法提示走 stderr。

    为什么分得这么清：`mat token --new | pbcopy` 是标准用法 —— 说明文字混进 stdout
    会被一起复制走（实测踩过）。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEN_PROMPT_MODE", raising=False)
    inv.save_state({"invite_code": "ABCD2345", "token": "tok-abc"})

    assert _run(["token"]) == 0

    cap = capsys.readouterr()
    assert cap.out.strip() == "tok-abc", "stdout 混进了别的东西"
    assert "GEN_API_KEY=tok-abc" in cap.err


def test_token_snippet_is_not_repeated_once_already_on_server(monkeypatch, tmp_path, capsys):
    """已经在用官方代理了就别再唠叨（提示的唯一调用方是 `mat token`）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    inv.save_state({"invite_code": "ABCD2345", "token": "tok-abc"})

    assert _run(["token"]) == 0

    cap = capsys.readouterr()
    assert cap.out.strip() == "tok-abc"
    assert "GEN_API_KEY" not in cap.err


# -- 概览页到账提示（§6.2 ⑤）------------------------------------------------


def test_notice_is_none_for_a_fresh_user():
    assert inv.notice() is None


def test_notice_reports_new_credited_count_then_goes_quiet():
    inv.save_state({"invite_code": "ABCD2345",
                    "ledger_cache": {"credited_this_month": 2, "monthly_cap": 4}})

    assert "新增 2 人" in (inv.notice() or "")

    inv.ack_notice()
    assert inv.notice() is None, "点过「知道了」就不该再冒 —— 不常驻是设计要求"


def test_notice_reports_pro_extension_once():
    inv.save_state({"invite_code": "ABCD2345",
                    "pro_claim": {"activated": True, "expires_at": 2000000000},
                    "ledger_cache": {"credited_this_month": 0, "monthly_cap": 3}})

    assert "Pro 有效期延长至" in (inv.notice() or "")

    inv.ack_notice()
    assert inv.notice() is None


def test_notice_is_silent_for_a_pending_or_skipped_claim():
    """没真的到账就不许报喜：`pending_sign`（没配签名密钥）与"本机已有更长期限"都不是到账。"""
    for claim in ({"activated": False, "reason": "本机已有更长的有效期"},
                  {"kind": "pro_days", "amount": 3, "status": "pending_sign"}):
        inv.save_state({"invite_code": "ABCD2345", "pro_claim": claim,
                        "ledger_cache": {"credited_this_month": 0}})
        assert inv.notice() is None, f"{claim} 不该被当成到账"


def test_quota_exceeded_message_points_to_invite():
    """额度触顶文案要带邀请入口 —— 用户**只在正缺额度的那一刻**才有动机去邀请（§6.2）。"""
    from insta_engine.quota import QuotaExceeded, UsageStore

    store = UsageStore()
    data = store._load()
    data["gen_count"] = 999
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(QuotaExceeded) as exc:
        store.ensure_capacity(1)

    assert "mat invite" in str(exc.value)


def test_ledger_surfaces_capped_count(monkeypatch, tmp_path, capsys):
    rec = Recorder(FakeResponse(payload={
        "user_id": "inv_1", "invited": 6, "qualified": 6, "pending": 0,
        "credited_this_month": 4, "monthly_cap": 4, "capped": 2,
        "reward_kind": "pro_days", "reward_amount": 3}))
    monkeypatch.setattr("requests.request", rec)
    monkeypatch.chdir(tmp_path)
    inv.save_state({"user_id": "inv_1", "invite_code": "ABCD2345", "token": "tok"})

    _run(["invite"])
    out = capsys.readouterr().out
    assert "超额未计酬" in out and "2 人" in out
    assert "Pro 时长 3 天" in out
