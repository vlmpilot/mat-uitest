"""定位策略别名归一化（2026-10-28）。

**起因**：本地 Qwen3-VL-8B 生成用例时输出 `"strategy": "desc"`，Pydantic 枚举校验
直接失败；内容级重试两次，模型仍写 `desc` ⇒ 整条用例作废。

这不是模型"笨"，而是枚举值的写法空间比模型见过的更大。归一化只做两件事：
同义词纠正（desc→description）与大小写不敏感；**表外未知值仍然报错**——
静默猜测会把"模型写错"变成"引擎点错"，比直接失败更难查。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from insta_engine.dsl import Locator, LocatorStrategy


@pytest.mark.parametrize(("raw", "expected"), [
    ("desc", LocatorStrategy.DESCRIPTION),
    ("Description", LocatorStrategy.DESCRIPTION),
    ("content-desc", LocatorStrategy.DESCRIPTION),
    ("resource-id", LocatorStrategy.RES_ID),
    ("res_id", LocatorStrategy.RES_ID),
    ("text_contains", LocatorStrategy.TEXT_CONTAINS),
    ("textContains", LocatorStrategy.TEXT_CONTAINS),
    ("image_template", LocatorStrategy.IMAGE_TEMPLATE),
    ("coordinates", LocatorStrategy.POINT),
    ("XPATH", LocatorStrategy.XPATH),
])
def test_strategy_aliases_are_normalized(raw, expected):
    assert Locator(strategy=raw, value="x").strategy is expected


def test_vision_alias_is_normalized_but_still_rejected():
    """`vision` 会归一成 `vision-llm`，但 DSL 本来就不允许声明它。

    这里要的是**有用的报错**：告诉用户用 `onFailure: heal`，
    而不是「vision 不是合法枚举」这种让人去猜的提示。
    """
    with pytest.raises(ValidationError, match="onFailure"):
        Locator(strategy="vision", value="x")


def test_unknown_strategy_still_fails():
    """表外值不猜：写错就该报错，而不是被映射成某个"看起来像"的策略。"""
    with pytest.raises(ValidationError):
        Locator(strategy="by-magic", value="x")


def test_alias_applies_to_fallback_chain():
    locator = Locator.model_validate({
        "strategy": "desc",
        "value": "搜索框",
        "fallback": {"strategy": "resource-id", "value": "com.demo:id/search"},
    })
    assert locator.strategy is LocatorStrategy.DESCRIPTION
    assert locator.fallback is not None
    assert locator.fallback.strategy is LocatorStrategy.RES_ID


def test_enum_instance_passes_through():
    """已经是枚举实例时不能被归一化逻辑破坏。"""
    locator = Locator(strategy=LocatorStrategy.TEXT, value="搜索")
    assert locator.strategy is LocatorStrategy.TEXT
