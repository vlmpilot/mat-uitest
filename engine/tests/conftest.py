import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_runner_sleep(monkeypatch):
    """runner 的等待全部走 _pause：测试注入 no-op（外部评审 P1-7，全量 <90s）。"""
    monkeypatch.setattr("insta_engine.runner._pause", lambda seconds: None)


@pytest.fixture(autouse=True)
def _no_ai_metrics(monkeypatch):
    """测试默认**关闭 AI 计量**：否则每个假请求都会往用户真实的 `~/.mat/metrics/ai.jsonl`
    追加一行 0ms/无 token 的记录，把真实数据搅浑（2026-10-21 实测：门禁跑完多了 50 行假数据）。

    需要验证计量的用例自己 `setenv("MAT_AI_METRICS", "1")` 并把 MAT_HOME 指到临时目录。
    """
    monkeypatch.setenv("MAT_AI_METRICS", "0")


@pytest.fixture(autouse=True)
def _tests_allow_stale_code(monkeypatch):
    """单测默认豁免「源码新鲜度」守卫（MAT_ALLOW_STALE_CODE=1）。

    守卫防的是**长驻 console** 用旧代码写盘（真实踩坑）；pytest 进程与被测源码
    同代，本不该被它管。且全量跑测期间外部进程（iCloud/Spotlight 同步）偶发以
    **相同内容**重写源码文件（实测 2026-10-19：__main__.py / wda_driver.py mtime
    被碰、内容零变化），守卫会把写盘型用例误判成「跑在旧代码上」→ 假红。
    专测 staleness 判定的两条用例自行 delenv/setenv，不受本 fixture 影响。
    """
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")

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


# -- P0-2 过渡：旧语义用例的定向跳过 -----------------------------------------
#
# `ensure_capacity` 已退化为纯快照（生成拦截归服务端 429），test_invite.py 里那条
# 断言「预检抛 QuotaExceeded」的用例随之失效。该文件内大量历史字典字面量会触发
# 安全扫描误判，本轮无法就地改写（同 invite.py 的 secret 先例：要改命名/措辞，
# 不是扫描）；新语义的等价断言已落在 test_v1.py::test_quota_gen_counts_but_never_blocks
# 与 test_import.py::test_convert_counts_half_step_never_blocks。
# 迁移完成后删掉本段（任务看板 T-待办：迁移 test_invite.py 配额用例）。
_LEGACY_QUOTA_TESTS = {
    "test_invite.py::test_quota_exceeded_message_points_to_invite",
}


def pytest_collection_modifyitems(config, items):
    if os.environ.get("MAT_RUN_LEGACY_QUOTA_TESTS") == "1":
        return  # 迁移核对时临时恢复运行（会红，预期内）
    skip = pytest.mark.skip(
        reason="P0-2：预检不再抛 QuotaExceeded（拦截归服务端 429）；"
               "等价断言已迁移至 test_v1/test_import，本用例待迁移后删除")
    for item in items:
        if f"{item.path.name}::{item.name}" in _LEGACY_QUOTA_TESTS:
            item.add_marker(skip)
