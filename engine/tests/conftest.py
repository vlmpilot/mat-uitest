import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_runner_sleep(monkeypatch):
    """runner 的等待全部走 _pause：测试注入 no-op（外部评审 P1-7，全量 <90s）。"""
    monkeypatch.setattr("insta_engine.runner._pause", lambda seconds: None)

# 让单测无需安装 engine 包即可运行（CI 与本地一致）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _unit_tests_never_call_real_adb(monkeypatch):
    """单测一律不打真机 adb。

    为什么必须打桩：`explore()` 每一轮循环都抓一次 logcat，而相关用例的时间预算只有
    `minutes=0.05`（3 秒）。真机上单次 `adb logcat -d -t 300` 平时约 0.1s，但 **adb server
    冷启动 / 设备繁忙时可达 7–10 秒（就是 logcat_tail 的 timeout）**，一次就吃光预算 →
    `explore` 只走 1 步、只记录 L0 页 → `test_arrivals_*` 与 `test_explore_*` 集体变红。

    危险之处在于**红灯取决于外部设备状态**：插着手机跑就红、拔了手机跑就绿。这种红灯会被
    读成「引擎回归了」，而真相是测试在偷偷依赖一个外部进程的延迟。所以在这里统一打桩。

    要测崩溃检测的用例，请自己覆写期望文本，例如：
        monkeypatch.setattr(explore, "logcat_tail", lambda *a, **k: "FATAL EXCEPTION: main")
    """
    import insta_engine.explore as explore

    monkeypatch.setattr(explore, "logcat_tail", lambda *a, **k: "")
    # T76：每轮开始时清一次 logcat（限定崩溃证据的时效）——单测里同样不能打真机。
    monkeypatch.setattr(explore, "logcat_clear", lambda *a, **k: True)
    # runner._capture_logcat 走 adb.logcat_tail（函数级 import，调用时解析→可桩）；
    # page_index.crawl 的等待走 page_index._pause——都不打真机 adb / 不真睡
    monkeypatch.setattr("insta_engine.adb.logcat_tail", lambda *a, **k: "")
    monkeypatch.setattr("insta_engine.adb.logcat_clear", lambda *a, **k: True)
    monkeypatch.setattr("insta_engine.page_index._pause", lambda s: None)
