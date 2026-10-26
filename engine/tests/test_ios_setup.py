"""iOS 真机体检（ios_setup）+ `--fresh-install` + `.app` 读 bundle id。

全部打桩：不碰真机、不跑 xcodebuild。守的是**判据**：
六道门的顺序、第一项失败就是"下一步"、Wi-Fi 设备要被判不通过、fresh-install 的入参校验。
"""

from types import SimpleNamespace

from insta_engine import ios_device, ios_setup
from insta_engine.__main__ import _fresh_install_ios

_OK_DEV = {"udid": "u1", "model": "iPhone 15", "os_version": "17.4",
           "transport": "wired", "developer_mode": True, "paired": True}


def _in_days(n: int):
    """测试用：n 天后的时刻（带时区，模拟 profile 的 ExpirationDate）。"""
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(days=n)


def _patch_green(monkeypatch, account=1, dev=_OK_DEV, wda=True, signed=True):
    monkeypatch.setattr(ios_setup, "_xcode_version", lambda: "26.5")
    monkeypatch.setattr(ios_setup, "signed_runner_exists", lambda: signed)
    monkeypatch.setattr(ios_setup, "_device_locked", lambda udid: False)
    monkeypatch.setattr(ios_setup, "_evidence_available", lambda: True)
    # cert-expiry 是条件项（无 profile 的机器上不出现）——全绿用例必须显式桩它，
    # 否则空 HOME / Linux CI 必红（2026-10-19 修复：此前漏桩导致 CI 与本机行为分叉）
    monkeypatch.setattr(ios_device, "profile_expiry",
                        lambda metas=None: {"name": "p", "app_id": "T.a",
                                            "expires": None, "days_left": 30})
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: account)
    monkeypatch.setattr(ios_device, "team_id", lambda: "TEAM123456")
    monkeypatch.setattr(ios_device, "pick_device", lambda serial=None: (dev, ""))
    monkeypatch.setattr(ios_setup, "_wda_ready", lambda udid: wda)


def test_all_green(monkeypatch):
    _patch_green(monkeypatch)
    items = ios_setup.checks()
    assert [c.id for c in items] == ["xcode", "account", "team", "cert-expiry",
                                     "device", "unlocked", "wda", "signed-runner",
                                     "evidence"]
    assert all(c.ok for c in items), [(c.id, c.hint) for c in items if not c.ok]
    assert "全部就绪" in ios_setup.render(items)


def test_account_missing_is_first_next_step(monkeypatch):
    """没有已签名产物时，账号是第二道门——失败时"下一步"必须是登录。"""
    _patch_green(monkeypatch, account=0, signed=False)
    items = ios_setup.checks()
    failed = [c for c in items if not c.ok]
    assert failed and failed[0].id == "account"
    assert "Accounts" in ios_setup.render(items)


def test_wifi_device_fails_with_usb_hint(monkeypatch):
    dev = dict(_OK_DEV, transport="localNetwork")
    _patch_green(monkeypatch, dev=dev)
    items = {c.id: c for c in ios_setup.checks()}
    assert not items["device"].ok and "数据线" in items["device"].hint


def test_developer_mode_off_fails(monkeypatch):
    dev = dict(_OK_DEV, developer_mode=False)
    _patch_green(monkeypatch, dev=dev)
    items = {c.id: c for c in ios_setup.checks()}
    assert not items["device"].ok and "开发者模式" in items["device"].hint


def test_no_device_skips_wda_with_reason(monkeypatch):
    monkeypatch.setattr(ios_setup, "signed_runner_exists", lambda: False)
    monkeypatch.setattr(ios_setup, "_xcode_version", lambda: "26.5")
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 1)
    monkeypatch.setattr(ios_device, "team_id", lambda: "T")
    monkeypatch.setattr(ios_device, "pick_device", lambda serial=None: (None, "没有已配对的 iOS 真机"))
    items = {c.id: c for c in ios_setup.checks()}
    assert not items["device"].ok and "没有已配对" in items["device"].hint
    assert "未检查" in items["wda"].label


def test_wda_not_ready_hint_mentions_trust_and_xcode(monkeypatch):
    """**没有已签名产物**时，WDA 未就绪要给出两个真实原因：没跑 ⌘U / 证书没信任。"""
    _patch_green(monkeypatch, wda=False, signed=False)
    items = {c.id: c for c in ios_setup.checks()}
    hint = items["wda"].hint
    assert "⌘U" in hint and "VPN 与设备管理" in hint


def test_run_json_and_exit_code(monkeypatch, capsys):
    _patch_green(monkeypatch, account=0, signed=False)
    code = ios_setup.run(as_json=True)
    out = capsys.readouterr().out
    assert code == 1 and '"checks"' in out and '"account"' in out


def test_bundle_id_of_reads_info_plist(tmp_path):
    import plistlib

    app = tmp_path / "Demo.app"
    app.mkdir()
    with (app / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": "com.example.demo"}, f)
    assert ios_device.bundle_id_of(app) == "com.example.demo"
    assert ios_device.bundle_id_of(tmp_path / "nope.app") is None


def test_fresh_install_requires_ios_and_app_path():
    args = SimpleNamespace(platform="android", app_path="/x.app", serial=None)
    assert _fresh_install_ios(args) == 2, "Android 不该走重装"
    args = SimpleNamespace(platform="ios", app_path=None, serial=None)
    assert _fresh_install_ios(args) == 2, "缺 --app-path 必须报错"


def test_fresh_install_uninstall_then_install(monkeypatch, tmp_path):
    calls: list[tuple] = []
    monkeypatch.setattr(ios_device, "pick_device", lambda serial=None: (_OK_DEV, ""))
    monkeypatch.setattr(ios_device, "bundle_id_of", lambda p: "com.example.demo")
    monkeypatch.setattr(ios_device, "uninstall_app",
                        lambda u, b: (calls.append(("un", b)), (True, ""))[1])
    monkeypatch.setattr(ios_device, "install_app",
                        lambda u, p: (calls.append(("in", p)), (True, ""))[1])
    args = SimpleNamespace(platform="ios", app_path=str(tmp_path / "Demo.app"), serial=None)
    assert _fresh_install_ios(args) == 0
    assert calls == [("un", "com.example.demo"), ("in", str(tmp_path / "Demo.app"))]


def test_fresh_install_install_failure_returns_2(monkeypatch, tmp_path):
    monkeypatch.setattr(ios_device, "pick_device", lambda serial=None: (_OK_DEV, ""))
    monkeypatch.setattr(ios_device, "bundle_id_of", lambda p: "com.example.demo")
    monkeypatch.setattr(ios_device, "uninstall_app", lambda u, b: (True, ""))
    monkeypatch.setattr(ios_device, "install_app", lambda u, p: (False, "签名不匹配"))
    args = SimpleNamespace(platform="ios", app_path=str(tmp_path / "Demo.app"), serial=None)
    assert _fresh_install_ios(args) == 2


def test_signed_runner_missing_hints_one_time_command_u(monkeypatch):
    """没有已签名产物 ⇒ 提示"按一次 ⌘U"，这是免账号自动启动的前提。"""
    _patch_green(monkeypatch, wda=False, signed=False)
    items = {c.id: c for c in ios_setup.checks()}
    row = items["signed-runner"]
    assert row.status == "warn" and "⌘U" in row.hint


def test_signed_runner_missing_is_ok_when_wda_already_ready(monkeypatch):
    """WDA 已经就绪时，没有已签名产物**不该**被算成失败项（用户此刻不需要它）。"""
    _patch_green(monkeypatch, wda=True, signed=False)
    items = {c.id: c for c in ios_setup.checks()}
    assert items["signed-runner"].ok is True


def test_days_left_pure():
    """剩余天数向下取整（免费证书 7 天，差 1 小时也算 0 天）。"""
    from datetime import UTC, datetime, timedelta

    from insta_engine.ios_device import days_left

    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    assert days_left(now + timedelta(days=7), now) == 7
    assert days_left(now + timedelta(hours=23), now) == 0
    assert days_left(now - timedelta(days=1), now) == -1


def test_profile_expiry_picks_earliest(monkeypatch):
    """多份 profile 时取**最早过期**的——那才是先坏掉的那个。"""
    from datetime import UTC, datetime, timedelta

    from insta_engine import ios_device

    now = datetime.now(UTC)
    metas = [
        {"Name": "later", "ExpirationDate": now + timedelta(days=30),
         "Entitlements": {"application-identifier": "T.com.a"}},
        {"Name": "sooner", "ExpirationDate": now + timedelta(days=2),
         "Entitlements": {"application-identifier": "T.com.b"}},
        {"Name": "no-date"},  # 没有过期时间 ⇒ 跳过，不参与比较
    ]
    got = ios_device.profile_expiry(metas)
    assert got["name"] == "sooner" and got["app_id"] == "T.com.b"
    assert got["days_left"] in (1, 2)   # 边界日可能跨 1 天，别写死


def test_ios_setup_warns_when_cert_expiring(monkeypatch):
    """证书 ≤3 天要 warn 并给"再按一次 ⌘U"的续期提示；有效期内**不给续期按钮**
    （Apple 有效期内不重签，点了必然「没有变化」——2026-10-22 发起人）。"""
    _patch_green(monkeypatch)
    monkeypatch.setattr(ios_device, "profile_expiry",
                        lambda metas=None: {"name": "p", "app_id": "T.a",
                                            "expires": _in_days(2), "days_left": 2})
    items = {c.id: c for c in ios_setup.checks()}
    item = items["cert-expiry"]
    assert item.status == "warn" and "⌘U" in item.hint
    assert item.fix is None and item.as_dict().get("fix") is None


def test_ios_setup_expired_cert_offers_renew(monkeypatch):
    """过期后才转红并给续期按钮。"""
    _patch_green(monkeypatch)
    monkeypatch.setattr(ios_device, "profile_expiry",
                        lambda metas=None: {"name": "p", "app_id": "T.a",
                                            "expires": _in_days(-1), "days_left": -1})
    items = {c.id: c for c in ios_setup.checks()}
    item = items["cert-expiry"]
    assert item.status == "fail" and item.fix == {"action": "renew-cert"}


def test_ios_setup_ok_when_cert_has_time(monkeypatch):
    _patch_green(monkeypatch)
    monkeypatch.setattr(ios_device, "profile_expiry",
                        lambda metas=None: {"name": "p", "app_id": "T.a",
                                            "expires": _in_days(6), "days_left": 6})
    items = {c.id: c for c in ios_setup.checks()}
    assert items["cert-expiry"].ok is True


def test_evidence_missing_warns_but_not_fails(monkeypatch):
    """pymobiledevice3 未装 ⇒ warn + 提示安装，但**不算失败项**（执行不受影响）。"""
    _patch_green(monkeypatch)
    monkeypatch.setattr(ios_setup, "_evidence_available", lambda: False)
    items = {c.id: c for c in ios_setup.checks()}
    row = items["evidence"]
    assert row.ok is True and row.status == "warn"
    assert "pymobiledevice3" in row.hint and "证据" in row.hint


def test_evidence_ready_is_ok(monkeypatch):
    _patch_green(monkeypatch)
    items = {c.id: c for c in ios_setup.checks()}
    assert items["evidence"].ok is True and items["evidence"].status == "ok"


# ---------------- 锁屏中止判据（2026-10-19：必须精确） ----------------

def test_is_device_locked_error_matches_only_real_lock():
    """只认真正的锁屏错误；提示文案里的"锁屏"不能触发整批中止。"""
    from types import SimpleNamespace

    from insta_engine.__main__ import _is_device_locked_error

    def run_with(err):
        return SimpleNamespace(steps=[SimpleNamespace(error=err)])

    assert _is_device_locked_error(run_with("RuntimeError: 设备锁屏：请解锁 iPhone")) is True
    assert _is_device_locked_error(
        run_with("WDAUnknownError: Timed out while waiting until the screen gets unlocked")) is True
    assert _is_device_locked_error(
        run_with("系统弹窗盖住前台…激活它会触发黑屏重载并锁屏。")) is False
    assert _is_device_locked_error(SimpleNamespace(steps=[])) is False


def test_locked_device_fails_with_actionable_hint(monkeypatch):
    """锁屏是运行期头号阻塞：必须单列一项，且提示"解锁 + 自动锁定设永不"。"""
    _patch_green(monkeypatch)
    monkeypatch.setattr(ios_setup, "_device_locked", lambda udid: True)
    items = {c.id: c for c in ios_setup.checks()}
    row = items["unlocked"]
    assert row.ok is False and "自动锁定" in row.hint


def test_lock_probe_unknown_is_skipped(monkeypatch):
    """查不到锁屏状态（Wi-Fi / 未装 wda）就不下结论——不能凭空报红。"""
    _patch_green(monkeypatch)
    monkeypatch.setattr(ios_setup, "_device_locked", lambda udid: None)
    assert "unlocked" not in {c.id for c in ios_setup.checks()}


def test_missing_account_is_not_blocking_when_signed_runner_exists(monkeypatch):
    """有已签名产物时，账号未登录**不该**被排成"下一步"——复用路径根本不用账号。

    2026-10-20 实测：账号 plist 是空的，但 15×3 跑得通（走复用）。
    旧实现把它标 ❌ ⇒ `render` 的"下一步"指去登录一个不需要的账号。
    """
    _patch_green(monkeypatch, account=0, signed=True)
    items = {c.id: c for c in ios_setup.checks()}
    row = items["account"]
    assert row.ok is True and row.status == "warn", "有产物时账号只算提示，不算失败"
    assert "可跳过" in row.label


def test_missing_account_blocks_when_no_signed_runner(monkeypatch):
    """没有已签名产物时账号是**真阻塞**（要构建就必须有账号）。"""
    _patch_green(monkeypatch, account=0, signed=False)
    items = {c.id: c for c in ios_setup.checks()}
    assert items["account"].ok is False


def test_wda_hint_points_to_start_when_signed_runner_exists(monkeypatch):
    """WDA 未就绪 + 有产物 ⇒ 提示 `--start`，而不是让人去按 ⌘U。"""
    _patch_green(monkeypatch, wda=False, signed=True)
    row = {c.id: c for c in ios_setup.checks()}["wda"]
    assert "--start" in row.hint and "⌘U" not in row.hint
