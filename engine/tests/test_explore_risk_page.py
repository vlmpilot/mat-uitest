"""撞到**风控/验证页**要立即终止本轮，并单独报出成因（T74）。

## 为什么要它

实测（2026-10-06/07）**4 轮 `stuck`（`no-candidates`）全部终结在风控页上**：
`JDRiskHandleActivity`、`YodaRouterTransparentActivity` / `身份核实`+滑块验证。

风控页只有 **2–7 个可点项**，试完就"候选耗尽"，然后**空转 12 次回退**（每轮 1–4 分钟）。
而报告只写 `stuck` ⇒ 用户会去**调 `--max-depth` / 改选点策略**，方向全错 ——
真实原因是 **App 的反自动化拦截**，不是产品缺陷，也不是探索策略的问题。

## 两个口径不能混（`risk.py` 里有对照表）

| 消费方 | 问的问题 | 漏报代价 | 误报代价 | 取向 |
|---|---|---|---|---|
| 判定脚本 `RISK_MARKERS` | 这轮任务被 App 拦了吗 | 风控被算成**产品缺陷** | 真失败被排除出分母 | 宁可多报 |
| 探索 `RISK_STRONG` | 这页值得立即终止本轮吗 | 继续空转 12 次回退 | **白砍一轮探索** | 宁可少报 |

所以本文件里**"弱特征单独出现不算"**是核心反例：某 App「登录帮助」页有
「收不到验证码」，只按关键词判会把**正常页面**误判成风控。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_engine.explore import (
    BLOCKED_CAUSE_RISK_CONTROL,
    STOP_BLOCKED,
    explore,
)
from insta_engine.risk import detect_risk_page

# ---------------------------------------------------------------- 纯函数判据

@pytest.mark.parametrize("xml_text", [
    "身份核实",              # 某 App（拼图验证页的标题）
    "请拖动下方滑块完成拼图",   # 某 App
    "请向右滑动滑块验证",      # 某 App（换个措辞也要命中）
    "安全验证",              # 某 App JDRiskHandleActivity
    "拖动箭头填充拼图",        # 某 App（图形验证）
])
def test_strong_markers_hit(xml_text):
    assert detect_risk_page("com.x.SomeActivity", xml_text) is not None


@pytest.mark.parametrize("xml_text", [
    "收不到验证码",          # ⚠️ 某 App「登录帮助」页的真实文案 —— 正常页面！
    "请输入图形验证码",
    "语音验证码",
])
def test_weak_markers_alone_do_not_hit(xml_text):
    """**核心反例**：弱特征单独出现**不算**风控——否则会白砍一轮探索。"""
    assert detect_risk_page("com.x.SomeActivity", xml_text) is None, (
        f"「{xml_text}」在正常登录/帮助流程里也会出现，不能据此终止探索")


def test_normal_page_does_not_hit():
    assert detect_risk_page("com.x.MainActivity", "首页 搜索 我的 推荐") is None


def test_activity_hint_hits_even_without_text():
    """专用风控页可能**一条文案都没有**（实测某 App Yoda 页：1 节点 / 0 文案）。"""
    assert detect_risk_page("com.jingdong.app.mall.bundle.jdrhsdk.JDRiskHandleActivity", "") \
        == "activity:riskhandle"


# ---------------------------------------------------------------- 端到端（假驱动）

class _Raw:
    def __init__(self, pkg: str, activity: str) -> None:
        self._pkg, self._act = pkg, activity

    def app_current(self) -> dict:
        return {"package": self._pkg, "activity": self._act}


class _Drv:
    """A 页有一个可点按钮；点了之后 B 页是**风控页**（B 页自己也有可点项）。"""

    serial = None

    def __init__(self, b_text: str, b_activity: str = "com.x.B") -> None:
        self.raw = _Raw("com.x", ".A")
        self.b_text, self.b_activity = b_text, b_activity
        self.on_b = False

    def foreground_app(self) -> dict:
        return self.raw.app_current() or {}

    def dump(self) -> str:
        if not self.on_b:
            return ('<hierarchy><node class="Button" text="去B" resource-id="com.x:id/go"'
                    ' clickable="true" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')
        return ('<hierarchy><node class="TextView" text="' + self.b_text +
                '" resource-id="com.x:id/b" clickable="true" bounds="[0,0][10,10]"'
                ' package="com.x"/></hierarchy>')

    def click(self, x: int, y: int) -> None:
        self.on_b = True
        self.raw = _Raw("com.x", self.b_activity)

    def press(self, key: str) -> None:
        pass

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        pass


def _run(tmp_path: Path, drv: _Drv, tag: str) -> dict:
    return explore(drv, "com.x", minutes=0.05, max_steps=60,
                   artifacts_dir=tmp_path / tag, max_depth=3, sleep_scale=0.001,
                   index_path=tmp_path / "com.x.json")


def test_risk_page_terminates_the_round(tmp_path):
    """撞到风控页 ⇒ `blocked` + `risk-control`，且**很快**结束（不空转 12 次回退）。"""
    r = _run(tmp_path, _Drv("身份核实"), "risk")
    assert r["stop_reason"] == STOP_BLOCKED
    assert r["blocked_cause"] == BLOCKED_CAUSE_RISK_CONTROL
    kinds = {f["type"] for f in r["findings"]}
    assert "blocked" in kinds and "stuck" not in kinds, (
        "撞风控不能报成 stuck（那会让人去调深度/改选点）")
    # 关键：**不能**是"空转 12 次回退"那种结束
    assert r["backoffs"] == 0, f"应当立刻终止，实际回退 {r['backoffs']} 次"


def test_normal_page_does_not_terminate(tmp_path):
    """**反例**：正常页面**不能**触发风控终止。"""
    r = _run(tmp_path, _Drv("首页 推荐 搜索"), "normal")
    assert r["blocked_cause"] != BLOCKED_CAUSE_RISK_CONTROL


def test_weak_marker_does_not_terminate(tmp_path):
    """**反例**：只有弱特征（「收不到验证码」这类）**不能**终止探索。"""
    r = _run(tmp_path, _Drv("收不到验证码"), "weak")
    assert r["blocked_cause"] != BLOCKED_CAUSE_RISK_CONTROL


# ---------------------------------------------------------------- 策略：stop / wait

class _ClearableDrv(_Drv):
    """到 B 页后**先**是风控页；被人"过掉"之后变成正常页。

    `clears_after` 控制第几次 dump 之后恢复（`None` = 永不恢复）。
    """

    def __init__(self, clears_after: int | None) -> None:
        super().__init__("身份核实")
        self.clears_after = clears_after
        self.dumps_on_b = 0

    def dump(self) -> str:
        if not self.on_b:
            return super().dump()
        self.dumps_on_b += 1
        if self.clears_after is not None and self.dumps_on_b > self.clears_after:
            return ('<hierarchy><node class="Button" text="正常页" resource-id="com.x:id/n"'
                    ' clickable="true" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')
        return super().dump()


def test_policy_parsing(monkeypatch):
    """配置解析：**写错要回落到安全的默认值**（不能因为配置错就永久挂住）。"""
    from insta_engine.explore import DEFAULT_RISK_WAIT_SECONDS, risk_policy, risk_wait_seconds

    monkeypatch.delenv("MAT_RISK_POLICY", raising=False)
    assert risk_policy() == "stop", "默认必须是 stop（无人值守场景挂起 = 整批卡死）"
    monkeypatch.setenv("MAT_RISK_POLICY", "WAIT")
    assert risk_policy() == "wait"
    monkeypatch.setenv("MAT_RISK_POLICY", "nonsense")
    assert risk_policy() == "stop", "写错的值要回落到 stop，不能当成 wait"

    monkeypatch.setenv("MAT_RISK_WAIT_SECONDS", "abc")
    assert risk_wait_seconds() == DEFAULT_RISK_WAIT_SECONDS, "解析失败要回落，不能抛也不能挂住"
    monkeypatch.setenv("MAT_RISK_WAIT_SECONDS", "-5")
    assert risk_wait_seconds() == 0.0


def test_wait_policy_continues_after_human_clears(tmp_path, monkeypatch):
    """`wait` + 人工过掉验证 ⇒ **继续本轮**（不是终止）。"""
    monkeypatch.setenv("MAT_RISK_POLICY", "wait")
    drv = _ClearableDrv(clears_after=1)     # 第一次 dump 是风控页，之后恢复
    r = _run(tmp_path, drv, "waitok")
    kinds = {f["type"] for f in r["findings"]}
    assert "risk-cleared" in kinds, f"必须留下 risk-cleared finding（实际 {kinds}）"
    assert r["blocked_cause"] != BLOCKED_CAUSE_RISK_CONTROL, "已恢复就不该再判终止"
    assert r["risk_policy"] == "wait", "报告里要写明当时用的是哪个策略（配置即口径）"


def test_wait_policy_times_out_and_stops(tmp_path, monkeypatch):
    """`wait` + **没人来过验证** ⇒ 超时后仍按 `stop` 终止（绝不永久挂住）。"""
    monkeypatch.setenv("MAT_RISK_POLICY", "wait")
    monkeypatch.setenv("MAT_RISK_WAIT_SECONDS", "0")   # 立刻超时，避免测试等 2 分钟
    drv = _ClearableDrv(clears_after=None)
    r = _run(tmp_path, drv, "waitto")
    assert r["stop_reason"] == STOP_BLOCKED
    assert r["blocked_cause"] == BLOCKED_CAUSE_RISK_CONTROL


def test_default_policy_is_stop(tmp_path, monkeypatch):
    """**默认**（不设环境变量）⇒ 立即终止，**不等待**。"""
    monkeypatch.delenv("MAT_RISK_POLICY", raising=False)
    r = _run(tmp_path, _ClearableDrv(clears_after=1), "default")
    assert r["blocked_cause"] == BLOCKED_CAUSE_RISK_CONTROL
    assert r["risk_policy"] == "stop"
