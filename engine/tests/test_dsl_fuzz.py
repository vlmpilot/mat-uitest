"""DSL 边界的性质测试（hypothesis）：畸形输入只能"被明确拒绝"，不能"把引擎搞崩"。

## 为什么用性质测试而不是再加几十个用例

列举式的用例只能覆盖"想到的"畸形输入。这里守的是**不变式**：

1. 包名/组件校验**通过**的值，必定不含路径分隔符、空白与 shell 元字符
   —— 它们会被拼进 `monkey -p <pkg>` / `am start -n <comp>`，通过即代表可安全拼接；
2. 包名/组件校验**不通过**时，抛的必须是 `ValueError`
   —— 抛别的（TypeError/AttributeError）说明校验逻辑本身会炸，调用方接不住；
3. `TestCase.model_validate` 对任意 JSON-like 输入，只能"成功"或抛 `ValidationError`
   —— 抛 TypeError/KeyError 意味着一条畸形用例能让 `mat run` 崩在解析阶段，
   而 CI 里跑的可能是别人 PR 里的用例。

没装 hypothesis 时整文件 skip（公开仓的 CI 不一定装开发依赖）。
"""

from __future__ import annotations

import re

import pytest

hypothesis = pytest.importorskip("hypothesis", reason="需要 hypothesis（pip install -e '.[dev]'）")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from insta_engine import pkgname  # noqa: E402
from insta_engine.dsl import TestCase as _TestCase  # noqa: E402

# 导入名避开 pytest 的 Test* 收集（它会尝试收集 TestCase 这个 pydantic 模型）

#: 拼进设备 shell 就危险的字符（与 pkgname 的拒绝集一致，这里独立写一份：
#: 测试不该复用实现里的正则，否则正则漏了测试也一起漏）
DANGEROUS = re.compile(r"[\\/\s;&|$`\"'(){}<>]")

FAST = settings(max_examples=120, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])


@FAST
@given(st.text(max_size=40))
def test_safe_package_name_passes_only_safe_values(raw):
    """通过校验 ⇒ 可以安全拼进 shell 与路径。"""
    try:
        out = pkgname.safe_package_name(raw)
    except ValueError:
        return                     # 拒绝是允许的，只看"通过的那些"是否真的安全
    assert not DANGEROUS.search(out), f"通过了校验却含危险字符：{out!r}（输入 {raw!r}）"
    assert not out.startswith(("/", "\\", "~")), f"通过了校验却是绝对路径：{out!r}"
    assert out not in (".", "..") and not out.endswith("."), f"通过了校验却是点段：{out!r}"


@FAST
@given(st.text(max_size=40))
def test_safe_package_name_rejects_with_value_error(raw):
    """不通过 ⇒ 必须是 ValueError（不能是别的异常）。"""
    try:
        pkgname.safe_package_name(raw)
    except ValueError:
        pass
    # 其他异常会让 hypothesis 直接判失败——这正是要守的


@FAST
@given(st.text(max_size=60), st.text(max_size=60))
def test_safe_component_invariants(raw_pkg, raw_activity):
    """组件校验同两条不变式；通过时 activity 段只含字母数字与 `._$`。"""
    comp = f"{raw_pkg}/{raw_activity}"
    try:
        out = pkgname.safe_component(comp)
    except ValueError:
        return
    assert "/" not in out.rsplit("/", 1)[-1], f"activity 段里不该再有斜杠：{out!r}"
    activity = out.rsplit("/", 1)[-1]
    assert re.fullmatch(r"[A-Za-z0-9._$]+", activity), f"activity 段含非法字符：{activity!r}"


JSON_LIKE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=20),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=12), children, max_size=4),
    max_leaves=6,
)


@FAST
@given(JSON_LIKE)
def test_model_validate_only_raises_validation_error(value):
    """任意 JSON-like 输入：要么校验通过，要么 ValidationError。不得抛别的。"""
    try:
        _TestCase.model_validate(value)
    except ValidationError:
        pass
    # TypeError / KeyError / AttributeError 等会让这条失败——那才是"崩"


@FAST
@given(JSON_LIKE)
def test_model_validate_json_bytes_also_only_validation_error(value):
    """`model_validate_json` 的入口同样只能抛 ValidationError（CLI 读文件走这条）。"""
    import json

    try:
        raw = json.dumps(value)
    except (TypeError, ValueError):
        return                     # NaN/Infinity 之类序列化不了，不是本测试的范围
    try:
        _TestCase.model_validate_json(raw)
    except ValidationError:
        pass


def test_launch_package_empty_is_rejected():
    """显式锁住最常见的误用：launch 没给 package。"""
    with pytest.raises(ValueError, match="非法包名"):
        pkgname.safe_package_name("")
