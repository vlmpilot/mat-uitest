"""视觉兜底额度：**官方代理限额、自带 API Key 限额不成立**（批 2，2026-10-23）。

背景：视觉兜底的 token 成本原先一律按档位在本地核销，于是自带 API Key 的用户
（用自己的 Key、自己付 token 钱）到第 21 次也被拦住——平台既没出钱、也没理由设限。
现在额度闸只对**官方代理**（`GEN_PROMPT_MODE=server`）生效，自带 API Key 只计数。

这组测试成对锁住两条语义，避免将来"为了防超支"把闸门又加到 BYOK 上。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from PIL import Image

from insta_engine import __main__ as cli
from insta_engine.dsl import TestCase
from insta_engine.quota import QuotaExceeded, UsageStore
from insta_engine.quota_policy import plan_limits
from insta_engine.runner import Runner


def _fill_usage(home, vision_count: float) -> None:
    """把本月视觉用量写到指定值（直接落盘，避免循环调用几十次）。"""
    month = datetime.now(UTC).strftime("%Y-%m")
    (home / "usage.json").write_text(
        f'{{"month": "{month}", "gen_count": 0, "vision_count": {vision_count}}}',
        encoding="utf-8",
    )


class _ShotDriver:
    """只提供 vision 路径需要的能力：截图 + 控件树。"""

    def screenshot_pil(self):
        return Image.new("RGB", (4, 4), "white")

    def dump(self) -> str:
        return "<hierarchy/>"


def _vision_step():
    case = TestCase.model_validate({
        "caseId": "vision_quota",
        "name": "视觉兜底额度",
        "target": {"package": "com.insta.demo"},
        "steps": [{
            "id": "s1", "intent": "点按登录", "action": "tap",
            "target": {"strategy": "res-id", "value": "com.insta.demo:id/login"},
        }],
    })
    return case.steps[0]


# -- 存储层：两种模式的行为成对 ------------------------------------------------

def test_byok_vision_counts_but_never_blocks(tmp_path, monkeypatch):
    """自带 API Key：超过策略上限**不抛**，只把用量记下来（计数用于 mat status 展示）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    limit = plan_limits("free")["vision"]
    store = UsageStore(tmp_path / "usage.json")

    for _ in range(limit + 3):
        store.check_vision(enforce=False)  # 不抛

    snap = store.snapshot()
    assert snap["vision_count"] == limit + 3, "自带 API Key 仍应如实计数，只是不拦"
    assert snap["gen_count"] == 0


def test_server_vision_still_blocks_at_limit(tmp_path, monkeypatch):
    """官方代理：到上限仍抛 QuotaExceeded（平台出钱，闸门必须留）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    limit = plan_limits("free")["vision"]
    store = UsageStore(tmp_path / "usage.json")

    for _ in range(limit):
        store.check_vision()  # 默认 enforce=True

    with pytest.raises(QuotaExceeded):
        store.check_vision()
    assert store.snapshot()["vision_count"] == limit, "被拒的那次不该计数"


# -- runner 层：额度耗尽后，BYOK 仍能定位 ---------------------------------------

def _stub_vision(monkeypatch):
    """让 vision 走"真实路径"：enabled=True、客户端不读 env、locate 触发 before_call。

    ⚠️ `before_call` 必须真的被调用——真实 `vision.locate` 在**调用模型前**触发额度核销
    （缓存命中才跳过）。stub 若不调它，测的就不是额度路径。
    """
    import insta_engine.vision as vision_mod

    def fake_locate(client, image, intent, target, cache_key=None,
                    tree_evidence=False, before_call=None):
        if before_call is not None:
            before_call()
        return (10, 20)

    monkeypatch.setattr(vision_mod, "enabled", lambda: True)
    monkeypatch.setattr(vision_mod, "default_client", lambda: object())
    monkeypatch.setattr(vision_mod, "locate", fake_locate)


def test_vision_locate_byok_ignores_quota(tmp_path, monkeypatch):
    """额度已耗尽 + 自带 API Key ⇒ 仍调模型并返回坐标，且用量 +1。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("GEN_PROMPT_MODE", "local")
    limit = plan_limits("free")["vision"]
    _fill_usage(tmp_path, limit)
    _stub_vision(monkeypatch)

    runner = Runner(_ShotDriver(), tmp_path)
    element = runner._vision_locate(_vision_step(), {})

    assert element is not None, "自带 API Key 不该被平台额度拦住"
    assert element.point == (10, 20)
    assert UsageStore(tmp_path / "usage.json").snapshot()["vision_count"] == limit + 1


def test_vision_locate_server_respects_quota(tmp_path, monkeypatch):
    """额度已耗尽 + 官方代理 ⇒ 优雅降级（返回 None + 原因含额度），不中断执行。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    limit = plan_limits("free")["vision"]
    _fill_usage(tmp_path, limit)
    _stub_vision(monkeypatch)

    runner = Runner(_ShotDriver(), tmp_path)
    element = runner._vision_locate(_vision_step(), {})

    assert element is None
    assert "额度" in runner.vision_reason, f"原因应指向额度，实际 {runner.vision_reason!r}"
    assert UsageStore(tmp_path / "usage.json").snapshot()["vision_count"] == limit


def test_vision_locate_local_endpoint_ignores_quota(tmp_path, monkeypatch):
    """额度已耗尽 + 官方代理 + **视觉端点在本机** ⇒ 照常定位，且不占平台额度。

    这是「生成走云端、兜底走本地小模型」的混合部署。端点在本机意味着平台没出 token
    钱，额度闸必须让路：否则本地兜底会把官方视觉额度吃光，额度满后连本地模型都被
    QuotaExceeded 拒掉——本地化唯一确定的收益（截图不出本机）就被自己废掉了。
    计数也一并不记：`mat status` 的视觉用量只该反映平台代付的部分。
    """
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    monkeypatch.setenv("GEN_VL_BASE_URL", "http://127.0.0.1:8080/v1")
    limit = plan_limits("free")["vision"]
    _fill_usage(tmp_path, limit)
    _stub_vision(monkeypatch)

    runner = Runner(_ShotDriver(), tmp_path)
    element = runner._vision_locate(_vision_step(), {})

    assert element is not None, "本地端点的兜底不该被平台额度拦住"
    assert element.point == (10, 20)
    assert UsageStore(tmp_path / "usage.json").snapshot()["vision_count"] == limit, \
        "本地端点不花平台的钱，不该计入平台视觉用量"


def test_vision_locate_remote_vl_endpoint_still_counts(tmp_path, monkeypatch):
    """反向保障：`GEN_VL_BASE_URL` 指向**外部**端点 ⇒ 仍按官方代理限额核销。

    防止「本地端点放行」被写成「配了 GEN_VL_BASE_URL 就放行」——那等于给云端端点
    开了免费通道，平台额度闸形同虚设。
    """
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    monkeypatch.setenv("GEN_VL_BASE_URL", "https://vl.example.com/v1")
    limit = plan_limits("free")["vision"]
    _fill_usage(tmp_path, limit)
    _stub_vision(monkeypatch)

    runner = Runner(_ShotDriver(), tmp_path)
    element = runner._vision_locate(_vision_step(), {})

    assert element is None
    assert "额度" in runner.vision_reason, f"外部端点仍需限额，实际 {runner.vision_reason!r}"


# -- mat status：按模式分叉，不把平台额度当限制展示 ------------------------------

def test_status_byok_reports_no_cap(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("GEN_PROMPT_MODE", "local")
    monkeypatch.setattr(cli, "_server_usage", lambda: None)

    cli.cmd_status(None)
    out = capsys.readouterr().out

    assert "自带 API Key" in out
    # 自带 API Key 没有「额度」这个概念（2026-10-24 口径）：两行都说「本站不设上限」
    assert out.count("本站不设上限") >= 2, f"生成与视觉两行都该说明，实际输出：\n{out}"
    assert "不占平台额度" not in out, "自带 API Key 不该再提「额度」（有额度就有上限的暗示）"
    assert f"/{plan_limits('free')['vision']}" not in out, "自带 API Key 不该出现平台额度分母"


def test_status_server_reports_quota(tmp_path, monkeypatch, capsys):
    month = datetime.now(UTC).strftime("%Y-%m")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    monkeypatch.setattr(cli, "_server_usage", lambda: {
        "plan": "pro", "month": month, "gen": {"used": 5, "limit": 2000},
    })

    cli.cmd_status(None)
    out = capsys.readouterr().out

    assert "5/2000" in out, f"官方代理应如实显示 used/limit：\n{out}"
    assert "官方代理" in out
