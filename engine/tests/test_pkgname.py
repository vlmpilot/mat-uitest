"""包名/组件闸口的直接单测（2026-10-24 评审 P1-1 补盲区：此前 0 直接测试，却是注入闸）。

`safe_package_name` / `safe_component` 是所有「用户输入拼进路径 / adb shell」的收口点——
它们失效即穿透，必须有独立于任何调用方的用例钉住行为。
"""

from __future__ import annotations

import pytest

from insta_engine.pkgname import safe_component, safe_package_name

# -- safe_package_name ------------------------------------------------------

@pytest.mark.parametrize("pkg", [
    "com.example.app",        # 标准形态
    "p",                      # 短包名（既有用例兼容口径，刻意宽松）
    "com.demo_1.a-b",         # 下划线/连字符段
])
def test_safe_package_accepts(pkg):
    assert safe_package_name(pkg) == pkg


@pytest.mark.parametrize("pkg", [
    "",                       # 空
    "/etc/passwd",            # 绝对路径（Path 拼接会重置基目录）
    "../x",                   # 相对逃逸
    "..",
    "a/b",
    "a\\b",
    "~/x",
    "com.a;input tap 1 1",    # shell 元字符（拼进 am/monkey/dumpsys 的注入面）
    "com.a$(id)",
    "com.a`id`",
    "com a",
    "com.a|x",
])
def test_safe_package_rejects(pkg):
    with pytest.raises(ValueError):
        safe_package_name(pkg)


# -- safe_component（activity / 完整组件） -----------------------------------

@pytest.mark.parametrize("comp", [
    ".MainActivity",                            # 纯短名（调用方补包名）
    "MainActivity",
    "com.example.app/.MainActivity",            # 标准完整组件
    "com.example.app/com.example.app.MainActivity",
    "com.example.app/.Main$Inner",              # Java 内部类
])
def test_safe_component_accepts(comp):
    assert safe_component(comp) == comp


@pytest.mark.parametrize("comp", [
    "",                                          # 空
    "com.a/.M; input tap 1 1",                   # 审查实证过的注入载荷（分号）
    "com.a/.M&&input keyevent 4",
    "com.a/.M|x",
    "com.a/.M$(id)",
    "com.a/.M`id`",
    "com.a/.M x",                                # 空白
    "/etc/x/.M",                                 # 前段不是合法包名（路径形态）
    "../a/.M",
    "com.a/",                                    # 后段为空
    "com.a/.M;input",
])
def test_safe_component_rejects(comp):
    with pytest.raises(ValueError):
        safe_component(comp)
