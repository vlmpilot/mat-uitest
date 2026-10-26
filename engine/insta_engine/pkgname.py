"""包名路径安全助手（2026-10-12 评审 A3）。

`Path(base) / f"{package}.json"` 里的 package 全程来自用户输入（用例 JSON / CLI 参数 /
HTTP body）。Python 里 `Path("/a") / "/etc/x"` = `/etc/x.json` —— 绝对路径会重置拼接
结果，`../x` 则直接越出 base 目录。此前只有 web_server 的 review_list/save 做了校验，
同族其余位置（api_explore、page_index、review、runner、__main__、mcp_server）裸奔。

放行口径刻意宽松（允许 "p" 这类短包名与下划线，兼容既有用例与测试）：
拒绝的是**形状上可造成危害**的值 —— 空值、路径分隔符、绝对路径、`.`/`..`、
以及空白与 shell 元字符（2026-10-20 评审 P1-1：package 还会拼进
`monkey -p <pkg>` / `dumpsys package <pkg>` 等设备 shell，只挡路径逃逸不够；
真实包名 / bundle id 只含字母数字与 `._-`，收紧不影响任何合法输入）。
"""

from __future__ import annotations

import re

# 允许的一般形态（宽松）：字母开头的点分段标识符；不符合再走安全兜底判断。
_LOOSE_RE = re.compile(r"^[A-Za-z][\w]*(\.[\w]+)+$")
# 显式拒绝：路径分隔符、反斜杠、以点/波浪线/盘符冒号开头（绝对路径）、纯点段。
_UNSAFE_CHARS = re.compile(r"[\\/]")
# 显式拒绝：空白与 shell 元字符（包名进设备 shell 拼接时的注入面）。
_SHELL_CHARS = re.compile(r"[\s;&|$`\"'(){}<>]")

ERR = "非法包名：{pkg!r}（应为 com.example.app 形式，且不含路径分隔符）"
ERR_COMPONENT = ("非法 activity/component：{comp!r}（应为 .MainActivity 或 "
                 "com.example.app/.MainActivity 形式，且不含空白与特殊字符）")

# component 的 activity 段只许字母数字与 `._$`（Java 内部类的 `$`）——白名单制，
# 与 safe_package_name 的黑名单互补：activity 会原样拼进 `am start -n <component>`。
_ACTIVITY_CHARS = re.compile(r"^[A-Za-z0-9._$]+$")


def safe_package_name(package: str) -> str:
    """校验包名并原样返回；不合法抛 ValueError。

    用作所有 `<base>/<package>.json` 路径拼接前的统一闸口 —— 一处收口，多处受益。
    """
    pkg = (package or "").strip()
    if not pkg:
        raise ValueError(ERR.format(pkg=package))
    # 绝对路径 / 家目录 / 盘符：Windows 盘符（c:）在 macOS/Linux 是合法相对目录名，
    # 但作为包名输入必属误用，一并拒绝。
    if pkg.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", pkg):
        raise ValueError(ERR.format(pkg=package))
    if _UNSAFE_CHARS.search(pkg) or _SHELL_CHARS.search(pkg):
        raise ValueError(ERR.format(pkg=package))
    if pkg in (".", "..") or pkg.endswith("."):
        raise ValueError(ERR.format(pkg=package))
    return pkg


def safe_component(activity: str) -> str:
    """校验 activity/component 并原样返回；不合法抛 ValueError（2026-10-24 评审 P1-1）。

    合法形态两种（与 `am start -n` 的 component 写法一致）：
    - 纯 activity 名：`.MainActivity` / `MainActivity`（由调用方或 resolve-activity 补包名）；
    - 完整组件：`com.example.app/.MainActivity`、`com.example.app/com.example.app.MainActivity`。
      含 `/` 时**前段必须过 safe_package_name**。
    activity 段只许字母数字与 `._$`——它会被拼进 adb shell 的 `am start -W -n` 交
    设备端 sh 执行，`com.a/.M; input tap 1 1` 这类值即命令注入。
    """
    comp = (activity or "").strip()
    if not comp:
        raise ValueError(ERR_COMPONENT.format(comp=activity))
    if "/" in comp:
        pkg, _, act = comp.partition("/")
        try:
            safe_package_name(pkg)
        except ValueError as exc:
            raise ValueError(f"component 前段{exc}") from exc
        if not act or not _ACTIVITY_CHARS.match(act):
            raise ValueError(ERR_COMPONENT.format(comp=activity))
    elif not _ACTIVITY_CHARS.match(comp):
        raise ValueError(ERR_COMPONENT.format(comp=activity))
    return comp


def is_safe_package(package: str) -> bool:
    """宽松判定版：只回答"能不能安全地拼进路径"，不抛异常。"""
    try:
        safe_package_name(package)
    except ValueError:
        return False
    return True


def is_well_formed_package(package: str) -> bool:
    """严格形态判定（com.example.app），供 DSL schema / 提示文案使用。"""
    return bool(_LOOSE_RE.match((package or "").strip()))
