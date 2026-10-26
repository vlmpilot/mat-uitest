"""真机 WDA 启动路径（ios_wda.start_on_device）：签名参数、bundle id 改写、错误分类。

全部打桩：不打真机、不跑 xcodebuild。这里守的是**判据**——
"个人团队用哪个 bundle id""构建失败怎么翻译成人话""没有团队时给什么提示"。
"""

from pathlib import Path
from unittest import mock

from insta_engine import ios_device, ios_wda


def test_default_runner_bundle_id_uses_team_prefix():
    """个人团队不能注册 com.facebook.*（全局唯一），必须用团队前缀。"""
    assert ios_wda.default_runner_bundle_id("U56RW5ZM8J") == \
        "com.u56rw5zm8j.WebDriverAgentRunner"


def _fake_wda_tree(tmp_path: Path) -> Path:
    """合成一个 pbxproj：iOS runner 两处（USES_XCTRUNNER）+ tvOS 一处（不能误改）。"""
    src = tmp_path / "WebDriverAgent"
    (src / "WebDriverAgent.xcodeproj").mkdir(parents=True)
    pbx = src / "WebDriverAgent.xcodeproj" / "project.pbxproj"
    pbx.write_text("\n".join([
        "/* iOS Debug */",
        "\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;",
        "\t\t\t\tPRODUCT_NAME = \"$(TARGET_NAME)\";",
        "\t\t\t\tUSES_XCTRUNNER = YES;",
        "/* iOS Release */",
        "\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;",
        "\t\t\t\tUSES_XCTRUNNER = YES;",
        "/* tvOS runner：不能被改 */",
        "\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;",
        "\t\t\t\tSDKROOT = appletvos;",
        "/* 别的 target：不能被改 */",
        "\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentLib;",
    ]), encoding="utf-8")
    return src


def test_patch_runner_bundle_id_only_touches_ios_runner(tmp_path, monkeypatch):
    src = _fake_wda_tree(tmp_path)
    monkeypatch.setattr(ios_wda, "WDA_SRC_DIR", src)
    changed = ios_wda._patch_runner_bundle_id("com.team.WebDriverAgentRunner")
    assert changed == 2, "只应改带 USES_XCTRUNNER 的两处（iOS Debug/Release）"

    text = (src / "WebDriverAgent.xcodeproj" / "project.pbxproj").read_text(encoding="utf-8")
    assert text.count("PRODUCT_BUNDLE_IDENTIFIER = com.team.WebDriverAgentRunner;") == 2
    assert text.count("PRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;") == 1, \
        "tvOS runner 必须保持原样"
    assert "com.facebook.WebDriverAgentLib" in text

    # 幂等：再改一次不动
    assert ios_wda._patch_runner_bundle_id("com.team.WebDriverAgentRunner") == 0


def test_classify_device_error_translates_to_actions():
    """构建失败必须翻译成用户能做的动作——不让用户看原始 stderr。"""
    assert "Apple ID" in ios_wda._classify_device_error(
        "error: No Accounts: Add a new account in Accounts settings.")
    assert "Personal Team" in ios_wda._classify_device_error(
        "error: No profiles for 'com.x.WebDriverAgentRunner.xctrunner' were found")
    assert "开发团队" in ios_wda._classify_device_error(
        "Signing for \"WebDriverAgentRunner\" requires a development team.")
    assert "数据线" in ios_wda._classify_device_error("Unable to connect to device")
    assert "VPN 与设备管理" in ios_wda._classify_device_error(
        "The application could not be launched because the Developer App Certificate is not trusted.")
    assert ios_wda._classify_device_error("something unexpected") == ""


def test_start_on_device_without_team_gives_actionable_hint(monkeypatch):
    # 隔离复用分支：否则本机真有已签名产物时会去起服务并等 90s（测试变慢且不确定）
    monkeypatch.setattr(ios_wda, "_try_reuse_signed_runner", lambda udid, wait: None)
    monkeypatch.setattr(ios_device, "team_id", lambda: None)
    ok, detail = ios_wda.start_on_device("u1")
    assert not ok and "Settings → Accounts" in detail


def test_start_on_device_build_failure_is_classified(monkeypatch, tmp_path):
    monkeypatch.setattr(ios_wda, "_try_reuse_signed_runner", lambda udid, wait: None)
    monkeypatch.setattr(ios_device, "team_id", lambda: "TEAM123456")
    monkeypatch.setattr(ios_wda, "_ensure_wda_source", lambda: None)
    monkeypatch.setattr(ios_wda, "WDA_DEVICE_DD", tmp_path / "dd")
    monkeypatch.setattr(ios_wda, "_patch_runner_bundle_id", lambda bid: 2)
    monkeypatch.setattr(ios_wda, "_build_wda_device",
                        lambda udid, team, bid: (_ for _ in ()).throw(
                            RuntimeError("Xcode 没登录 Apple ID——Xcode → Settings → Accounts 登录后重试")))
    ok, detail = ios_wda.start_on_device("u1")
    assert not ok and "Apple ID" in detail


def test_start_on_device_success_path_passes_signing_params(monkeypatch, tmp_path):
    """成功路径：团队从证书取 → 改写 bundle id → 构建 → 常驻 → 轮询。"""
    monkeypatch.setattr(ios_wda, "_try_reuse_signed_runner", lambda udid, wait: None)
    monkeypatch.setattr(ios_device, "team_id", lambda: "TEAM123456")
    monkeypatch.setattr(ios_wda, "_ensure_wda_source", lambda: None)
    monkeypatch.setattr(ios_wda, "WDA_DEVICE_DD", tmp_path / "dd")
    seen: dict = {}
    monkeypatch.setattr(ios_wda, "_patch_runner_bundle_id",
                        lambda bid: seen.setdefault("bundle", bid) and 2)
    monkeypatch.setattr(ios_wda, "_build_wda_device",
                        lambda udid, team, bid: (seen.update(udid=udid, team=team),
                                                 tmp_path / "x.xctestrun")[1])
    monkeypatch.setattr(ios_wda, "_start_wda_device_service",
                        lambda udid, xr: seen.update(started=str(xr)))
    monkeypatch.setattr(ios_wda, "_poll_device_status", lambda udid, sec: True)

    ok, detail = ios_wda.start_on_device("u1")
    assert ok and "已就绪" in detail
    assert seen["bundle"] == "com.team123456.WebDriverAgentRunner"
    assert seen["team"] == "TEAM123456" and seen["udid"] == "u1"
    assert seen["started"].endswith("x.xctestrun")


def test_start_on_device_no_build_artifact_fast_path(monkeypatch, tmp_path):
    """allow_build=False 且没有产物 ⇒ 明确提示，不偷偷构建。"""
    monkeypatch.setattr(ios_wda, "_try_reuse_signed_runner", lambda udid, wait: None)
    monkeypatch.setattr(ios_device, "team_id", lambda: "TEAM123456")
    monkeypatch.setattr(ios_wda, "_ensure_wda_source", lambda: None)
    monkeypatch.setattr(ios_wda, "WDA_DEVICE_DD", tmp_path / "empty")
    monkeypatch.setattr(ios_wda, "_patch_runner_bundle_id", lambda bid: 0)
    ok, detail = ios_wda.start_on_device("u1", allow_build=False)
    assert not ok and "ios-setup" in detail


# ---- 复用 Xcode 已签名产物（2026-10-18：免账号/免 ⌘U 起 WDA） -------------------

def _fake_template(tmp_path):
    import plistlib

    data = {
        "WebDriverAgentRunner": {
            "TestHostPath": "__TESTROOT__/Debug-iphonesimulator/WebDriverAgentRunner-Runner.app",
            "TestHostBundleIdentifier": "com.facebook.WebDriverAgentRunner.xctrunner",
            "IsUITestBundle": True,
            "DependentProductPaths": [
                "__TESTROOT__/Debug-iphonesimulator/WebDriverAgentLib.framework"],
            "TestingEnvironmentVariables": {
                "DYLD_FRAMEWORK_PATH": "__TESTROOT__/Debug-iphonesimulator:"
                                       "__PLATFORMS__/iPhoneSimulator.platform/Developer/Library/Frameworks",
                "DYLD_INSERT_LIBRARIES": "__SIMRUNTIMEROOT__/usr/lib/libMainThreadChecker.dylib",
                "XCODE_SCHEME_NAME": "WebDriverAgentRunner",
            },
        },
        "__xctestrun_metadata__": {"FormatVersion": 1},
    }
    p = tmp_path / "template.xctestrun"
    p.write_bytes(plistlib.dumps(data))
    return p


def test_craft_device_xctestrun_rewrites_paths_and_bundle(tmp_path):
    """改造必须改对三处：路径 sim→device、bundle id 用产物实际值、去掉模拟器专用注入。"""
    import plistlib

    tmpl = _fake_template(tmp_path)
    out = ios_wda.craft_device_xctestrun(
        tmp_path / "App.app", "com.team.WebDriverAgentRunner.xctrunner", tmpl,
        tmp_path / "out.xctestrun")
    cfg = plistlib.loads(out.read_bytes())["WebDriverAgentRunner"]

    assert cfg["TestHostPath"] == "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    assert cfg["TestHostBundleIdentifier"] == "com.team.WebDriverAgentRunner.xctrunner"
    env = cfg["TestingEnvironmentVariables"]
    assert "iPhoneOS.platform" in env["DYLD_FRAMEWORK_PATH"]
    assert "Debug-iphoneos" in env["DYLD_FRAMEWORK_PATH"]
    assert "DYLD_INSERT_LIBRARIES" not in env, "模拟器专用注入必须去掉，否则真机启动即崩"
    assert cfg["DependentProductPaths"][0].startswith("__TESTROOT__/Debug-iphoneos/")


def test_find_signed_device_runner_requires_profile_and_xctrunner(tmp_path, monkeypatch):
    """判据两条都要满足：有 embedded.mobileprovision + Identifier 以 .xctrunner 结尾。"""
    base = tmp_path / "WebDriverAgent-abc" / "Build" / "Products" / "Debug-iphoneos"
    app = base / "WebDriverAgentRunner-Runner.app"
    app.mkdir(parents=True)
    monkeypatch.setattr(ios_wda, "_DERIVED_DATA", tmp_path)

    assert ios_wda.find_signed_device_runner() is None, "没有 profile 不算已签名产物"

    (app / "embedded.mobileprovision").write_bytes(b"x")
    monkeypatch.setattr(ios_wda.subprocess, "run", lambda *a, **k: mock.Mock(
        stderr="Identifier=com.team.WebDriverAgentRunner.xctrunner\n", returncode=0))
    found = ios_wda.find_signed_device_runner()
    assert found and found[1].endswith(".xctrunner")


def test_start_on_device_prefers_reuse_without_team(monkeypatch):
    """有已签名产物时**不该**再要求团队/账号——这是本机唯一能自动化的路径。"""
    monkeypatch.setattr(ios_wda, "_try_reuse_signed_runner",
                        lambda udid, wait: (True, "复用已签名产物"))
    called = {"team": 0}
    monkeypatch.setattr(ios_device, "team_id", lambda: called.__setitem__("team", 1))
    ok, detail = ios_wda.start_on_device("u1")
    assert ok and "复用" in detail
    assert called["team"] == 0, "复用路径不应去读团队（不需要账号）"


def test_find_signed_runner_skips_expired_and_picks_newest(tmp_path, monkeypatch):
    """多份产物时：**排除过期**，再按 mtime 取最新。

    2026-10-20 实测的 bug：旧实现 sorted() 取第一个，挑中了 9 月那份已过期产物
    （com.vizenluan.*，profile 9-25 过期），当天有效的 com.u56rw5zm8j.* 被忽略。
    """
    import os
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)

    def make(hashname: str, ident: str, expires: datetime, mtime: float):
        app = (tmp_path / f"WebDriverAgent-{hashname}" / "Build" / "Products"
               / "Debug-iphoneos" / "WebDriverAgentRunner-Runner.app")
        app.mkdir(parents=True)
        (app / "embedded.mobileprovision").write_bytes(b"x")
        os.utime(app, (mtime, mtime))
        return app

    make("aaa", "com.old.WebDriverAgentRunner.xctrunner",
         now - timedelta(days=25), 1_000_000)   # 过期产物：必须被排除
    new = make("zzz", "com.new.WebDriverAgentRunner.xctrunner",
               now + timedelta(days=4), 2_000_000)
    monkeypatch.setattr(ios_wda, "_DERIVED_DATA", tmp_path)
    monkeypatch.setattr(ios_wda, "_runner_identifier",
                        lambda app: ("com.old.WebDriverAgentRunner.xctrunner"
                                     if "aaa" in str(app)
                                     else "com.new.WebDriverAgentRunner.xctrunner"))
    monkeypatch.setattr(ios_wda, "_runner_profile_expiry",
                        lambda app: (now - timedelta(days=25)) if "aaa" in str(app)
                        else (now + timedelta(days=4)))

    got = ios_wda.find_signed_device_runner()
    assert got is not None and got[1] == "com.new.WebDriverAgentRunner.xctrunner", \
        f"应选未过期的最新产物，实际选了 {got}"
    assert str(new) in str(got[0])


# ---- 证书续期（2026-10-20） ---------------------------------------------------

def _renew_env(monkeypatch, before_days, after_days, build_exc=None):
    """打桩续期的外部依赖：源码/改写 bundle id/构建/团队/证书查询。"""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    state = {"n": 0}

    def fake_expiry(metas=None):
        state["n"] += 1
        days = before_days if state["n"] == 1 else after_days
        return {"name": "p", "app_id": "T.a", "expires": now + timedelta(days=days),
                "days_left": days}

    monkeypatch.setattr(ios_wda, "_ensure_wda_source", lambda: None)
    monkeypatch.setattr(ios_wda, "_patch_runner_bundle_id", lambda bid: 2)
    if build_exc is None:
        monkeypatch.setattr(ios_wda, "_build_wda_device", lambda udid, team, bid: Path("/tmp/x"))
    else:
        def boom(udid, team, bid):
            raise build_exc
        monkeypatch.setattr(ios_wda, "_build_wda_device", boom)
    monkeypatch.setattr("insta_engine.ios_device.profile_expiry", fake_expiry)


def test_renew_reports_success_only_when_expiry_actually_moves(monkeypatch):
    """构建返回 0 ≠ 续期成功：必须比较续期前后的剩余天数。"""
    _renew_env(monkeypatch, before_days=3, after_days=3)   # 有效期没动
    ok, detail = ios_wda.renew_device_profile("u1", team="T")
    assert ok is False and "没有变化" in detail


def test_renew_reports_success_when_expiry_extends(monkeypatch):
    _renew_env(monkeypatch, before_days=0, after_days=7)
    ok, detail = ios_wda.renew_device_profile("u1", team="T")
    assert ok is True and "已签发新证书" in detail


def test_renew_explains_no_accounts(monkeypatch):
    """xcodebuild 用不了账号时，必须给出手动路径（这一点代码绕不过）。"""
    _renew_env(monkeypatch, before_days=-1, after_days=-1,
               build_exc=RuntimeError("真机构建失败：No Accounts"))
    ok, detail = ios_wda.renew_device_profile("u1", team="T")
    assert ok is False and "⌘U" in detail


def test_profile_expiry_prefers_valid_over_expired():
    """残留的过期 profile 不该盖过有效的那份（否则体检会误报"已过期"）。"""
    from datetime import UTC, datetime, timedelta

    from insta_engine import ios_device

    now = datetime.now(UTC)
    metas = [
        {"Name": "old", "ExpirationDate": now - timedelta(days=25),
         "Entitlements": {"application-identifier": "T.old"}},
        {"Name": "live", "ExpirationDate": now + timedelta(days=4),
         "Entitlements": {"application-identifier": "T.live"}},
    ]
    got = ios_device.profile_expiry(metas)
    assert got["name"] == "live" and got["days_left"] >= 3


def test_profile_expiry_marks_all_expired():
    from datetime import UTC, datetime, timedelta

    from insta_engine import ios_device

    now = datetime.now(UTC)
    metas = [
        {"Name": "a", "ExpirationDate": now - timedelta(days=9),
         "Entitlements": {"application-identifier": "T.a"}},
        {"Name": "b", "ExpirationDate": now - timedelta(days=2),
         "Entitlements": {"application-identifier": "T.b"}},
    ]
    got = ios_device.profile_expiry(metas)
    assert got["expired"] is True and got["name"] == "b", "全过期时报最晚过期的那份"


# ---- 提前换新（--renew --force）：必须可回滚 ----------------------------------

def _force_env(tmp_path, monkeypatch, *, accounts: int, after_days: int,
               build_exc: Exception | None = None):
    """搭一个只有一份 runner profile 的假环境。"""
    from datetime import UTC, datetime, timedelta

    prof_dir = tmp_path / "profiles"
    prof_dir.mkdir()
    prof = prof_dir / "runner.mobileprovision"
    prof.write_bytes(b"dummy")

    now = datetime.now(UTC)
    monkeypatch.setattr(ios_wda, "_PROFILE_BACKUP_DIR", tmp_path / "backup")
    monkeypatch.setattr("insta_engine.ios_device._PROFILE_DIRS", (prof_dir,))
    monkeypatch.setattr(ios_wda, "_runner_profile_files", lambda team: [prof])
    monkeypatch.setattr("insta_engine.ios_device.xcode_account_count", lambda: accounts)
    monkeypatch.setattr(ios_wda, "_ensure_wda_source", lambda: None)
    monkeypatch.setattr(ios_wda, "_patch_runner_bundle_id", lambda bid: 1)
    if build_exc is None:
        monkeypatch.setattr(ios_wda, "_build_wda_device", lambda u, t, b: Path("/tmp/x"))
    else:
        def boom(u, t, b):
            raise build_exc
        monkeypatch.setattr(ios_wda, "_build_wda_device", boom)
    monkeypatch.setattr("insta_engine.ios_device.profile_expiry",
                        lambda metas=None: {"name": "p", "app_id": "T.a",
                                            "expires": now + timedelta(days=after_days),
                                            "days_left": after_days})
    return prof


def test_force_renew_refuses_without_account_and_deletes_nothing(tmp_path, monkeypatch):
    """没账号时**拒绝**并保持原样：删了 profile 又拿不到新的，会让设备立刻不可用。"""
    prof = _force_env(tmp_path, monkeypatch, accounts=0, after_days=3)
    ok, detail = ios_wda.renew_device_profile("u1", team="T", force=True)
    assert ok is False and "拒绝" in detail
    assert prof.exists(), "拒绝路径绝不能删 profile"


def test_force_renew_rolls_back_when_build_fails(tmp_path, monkeypatch):
    prof = _force_env(tmp_path, monkeypatch, accounts=1, after_days=3,
                      build_exc=RuntimeError("真机构建失败：something"))
    ok, detail = ios_wda.renew_device_profile("u1", team="T", force=True)
    assert ok is False
    assert prof.exists(), "构建失败必须把旧 profile 放回去"


def test_force_renew_rolls_back_when_expiry_not_extended(tmp_path, monkeypatch):
    """换新后有效期没变（Apple 没重发）⇒ 同样要回滚，不能留下"删了但没换来"。"""
    prof = _force_env(tmp_path, monkeypatch, accounts=1, after_days=3)
    # 让 before/after 都是 3 天：第一次 3，第二次 3 ⇒ 判定未延长
    ok, detail = ios_wda.renew_device_profile("u1", team="T", force=True)
    assert ok is False and "没有变化" in detail
    assert prof.exists(), "未延长时必须回滚"


def test_force_renew_keeps_new_profile_when_extended(tmp_path, monkeypatch):
    prof = _force_env(tmp_path, monkeypatch, accounts=1, after_days=7)
    # before 用 3 天、after 用 7 天
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    calls = {"n": 0}

    def expiry(metas=None):
        calls["n"] += 1
        days = 3 if calls["n"] == 1 else 7
        return {"name": "p", "app_id": "T.a", "expires": now + timedelta(days=days),
                "days_left": days}

    monkeypatch.setattr("insta_engine.ios_device.profile_expiry", expiry)
    ok, detail = ios_wda.renew_device_profile("u1", team="T", force=True)
    assert ok is True and "已签发新证书" in detail
    assert not prof.exists(), "换新成功时旧 profile 应保持已删除（备份里有一份）"
