"""自愈「点空了也算成功」的记账修正（2026-10-27）。

生产数据挖出来的：工作区 2306 份 run.json / 15128 步骤里，`vision-llm` 自愈成功
35 次，其中 **7 次（20%）** 带着「自愈动作后控件树无变化」——点了坐标、界面纹丝
不动，步骤却标 `passed`。报告写"已兜底命中"，人以为那一步真做了事。

旧行为（runner.py）：`after_heal == before_heal` 只往 warning 追加一句，
`result.status` 照样 `passed`。新行为：先补一次观察窗口（0.3s 对慢界面偏短），
仍无变化 ⇒ 判 failed，并保留 heal 提案（它仍有回写价值）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_engine.dsl import TestCase as _TestCase
from insta_engine.locator import Element
from insta_engine.runner import Runner


class _NoopDriver:
    """find 永远不存在（逼出 LocateFailed）；dump 由 `pages` 控制是否变化。"""

    serial = None
    platform = "android"

    def __init__(self, pages=None):
        self.pages = pages or ["<hierarchy/>"]
        self.i = 0

    def dump(self):
        xml = self.pages[min(self.i, len(self.pages) - 1)]
        self.i += 1
        return xml

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def wake_and_unlock(self):
        return None

    def start_keepalive(self):
        return None

    def stop_keepalive(self):
        return None

    def start_evidence(self):
        return None

    def foreground_app(self):
        return {"package": "com.x"}

    def find(self, by, value, timeout):
        class _N:
            exists = False

            def wait(self, timeout=0, raise_error=False):
                return False

        return _N()


def _case() -> _TestCase:
    return _TestCase.model_validate({
        "caseId": "selfheal_noop",
        "name": "自愈空点击",
        "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "tap",
                   "target": {"strategy": "text", "value": "不存在的文案"}}],
    })


def _prep(monkeypatch, drv, act_effect=None):
    """让自愈链一定走到本地模糊命中 + 动作由 `act_effect` 决定界面是否变化。"""
    import insta_engine.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_pause", lambda _s: None)  # 免等 1s

    def fake_heal(drv_, locator):
        return Element(strategy="text", value="兜底命中", point=(100, 100), drv=drv_)

    monkeypatch.setattr(runner_mod, "heal_fuzzy", fake_heal)

    if act_effect is not None:
        monkeypatch.setattr(Runner, "_act", act_effect)
    return Runner(drv, Path("/tmp/mat_selfheal_test"))


def test_noop_heal_is_not_counted_as_success(monkeypatch):
    """点击后控件树哈希不变 ⇒ 必须判 failed，且 error 说清"界面无变化"。"""
    drv = _NoopDriver()  # dump 恒定 ⇒ 前后哈希相同（就是"点空了"的形态）
    runner = _prep(monkeypatch, drv, act_effect=lambda self, step, case, el, v: None)

    step_result = runner.run(_case()).steps[0]

    assert step_result.status == "failed", "点空了不能算自愈成功"
    assert "界面无任何变化" in (step_result.error or ""), step_result.error
    assert "控件树哈希未变" in (step_result.error or "")
    # 提案仍要保留：它是待人工回写的有价值候选，只是这次没被采纳
    assert step_result.heal and step_result.heal["via"] == "local-fuzzy"
    assert "提案已保留" in (step_result.warning or "")


def test_heal_with_real_change_still_passes(monkeypatch):
    """界面确实变了（哈希不同）⇒ 仍走原来的成功路径，不能把正常自愈误判成失败。"""
    drv = _NoopDriver()
    # 每次 dump 给不同内容：模拟点击后界面真的变了
    counter = {"n": 0}

    def dump():
        counter["n"] += 1
        return f"<hierarchy n={counter['n']}/>"

    drv.dump = dump
    runner = _prep(monkeypatch, drv, act_effect=lambda self, step, case, el, v: None)

    step_result = runner.run(_case()).steps[0]

    assert step_result.status == "passed", step_result.error
    assert "兜底命中" in (step_result.warning or "")
    assert "界面无任何变化" not in (step_result.error or "")


def test_extra_settle_window_is_used_before_verdict(monkeypatch):
    """判定前必须多等一段：0.3s 对慢界面偏短，直接判死会误伤正常自愈。"""
    import insta_engine.runner as runner_mod

    paused = []
    monkeypatch.setattr(runner_mod, "_pause", lambda s: paused.append(s))

    drv = _NoopDriver()
    monkeypatch.setattr(runner_mod, "heal_fuzzy",
                        lambda d, loc: Element(strategy="text", value="x",
                                               point=(10, 10), drv=d))
    monkeypatch.setattr(Runner, "_act", lambda self, step, case, el, v: None)
    Runner(drv, Path("/tmp/mat_selfheal_test")).run(_case())

    assert 0.3 in paused, paused
    assert runner_mod.SELF_HEAL_SETTLE_EXTRA_SECONDS in paused, paused
    assert runner_mod.SELF_HEAL_SETTLE_EXTRA_SECONDS >= 0.5, "补测窗口太短等于没补"


@pytest.mark.parametrize("payload,expect_reject", [
    ({"found": True, "confidence": 0.95, "x": 2000, "y": 3000}, True),   # 严格越界
    ({"found": True, "confidence": 0.95, "x": 0.5, "y": 0.5}, False),    # 正常比例
    ({"found": True, "confidence": 0.95, "x": 1, "y": 400}, False),      # 贴边但不越界
])
def test_out_of_screen_coords_rejected(payload, expect_reject):
    """屏幕外坐标必须拒（不点屏幕外）；贴边不拒（全屏图片的合法中心就在边上）。"""
    import io

    from PIL import Image

    from insta_engine import vision

    buf = io.BytesIO()
    Image.new("RGB", (400, 800), "white").save(buf, format="PNG")
    png = buf.getvalue()

    pos = vision.parse_vision_response(payload, png)
    if expect_reject:
        assert pos is None
        assert "超出屏幕" in vision.last_result()["reason"]
    else:
        assert pos is not None


# -- 自绘界面：树不动但画面动过，不能判成点空（2026-10-27 真机回归） ----------------
#
# 现场：canvas 靶场上视觉兜底点中「选项丙」，画面明显变绿、文字变成「已点击」，
# 但 canvas 的内容**不进入 accessibility 树** ⇒ 树哈希纹丝不动 ⇒ 被判成点空。
# 视觉兜底最该起作用的场景（WebView / Flutter / 自绘），恰好就是树不反映内容的场景。
# 修法：这类坐标点击额外留一张画面指纹，树没变时用它做第二判据。


class _FrameDriver(_NoopDriver):
    """dump 恒定（模拟自绘界面：树不反映内容）；screenshot_pil 按 frames 依次返回。"""

    def __init__(self, frames):
        super().__init__()
        self.frames = frames
        self.fi = 0

    def screenshot_pil(self):
        frame = self.frames[min(self.fi, len(self.frames) - 1)]
        self.fi += 1
        return frame


def _img(color):
    from PIL import Image

    return Image.new("RGB", (400, 800), color)


def _prep_vision(monkeypatch, drv):
    """绕开零成本的 local-fuzzy，直接落到 vision-llm（按坐标点击的那条路）。"""
    import insta_engine.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_pause", lambda _s: None)
    monkeypatch.setattr(runner_mod, "heal_fuzzy", lambda d, loc: None)  # 让路给视觉兜底
    monkeypatch.setattr(Runner, "_vision_allowed", lambda self, step: True)
    monkeypatch.setattr(
        Runner, "_vision_locate",
        lambda self, step, variables: Element(strategy="vision-llm", value="(100,100)",
                                             point=(100, 100), drv=drv))
    monkeypatch.setattr(Runner, "_act", lambda self, step, case, el, v: None)
    return Runner(drv, Path("/tmp/mat_selfheal_vision"))


def test_tree_static_but_frame_changed_is_success(monkeypatch):
    """树没变、画面变了 ⇒ 自绘界面上点中了，必须 passed（否则主战场全是假红）。"""
    drv = _FrameDriver([_img("white"), _img("black")])  # 前后画面明显不同
    step_result = _prep_vision(monkeypatch, drv).run(_case()).steps[0]

    assert step_result.status == "passed", step_result.error
    assert step_result.used_strategy == "vision-llm"
    assert "画面已改变" in (step_result.warning or ""), step_result.warning


def test_tree_and_frame_both_static_is_failure(monkeypatch):
    """树没变、画面也没变 ⇒ 真的点空了，仍判 failed（P0 的判断力不能被削弱）。"""
    drv = _FrameDriver([_img("white"), _img("white")])
    step_result = _prep_vision(monkeypatch, drv).run(_case()).steps[0]

    assert step_result.status == "failed", "画面也没变就说明真没点到"
    assert "画面指纹一致" in (step_result.error or ""), step_result.error
    assert step_result.heal and step_result.heal["via"] == "vision-llm"


def test_frame_unavailable_falls_back_to_failure(monkeypatch):
    """截图拿不到 ⇒ 无法判断，按未变化处理（不能凭"没证据"就记成功）。"""
    drv = _FrameDriver([_img("white")])

    def boom():
        raise RuntimeError("screencap 失败")

    drv.screenshot_pil = boom
    step_result = _prep_vision(monkeypatch, drv).run(_case()).steps[0]

    assert step_result.status == "failed"
    assert "画面指纹不可用" in (step_result.error or ""), step_result.error

