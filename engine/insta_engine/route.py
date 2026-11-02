"""生成请求走哪一侧：本站自部署的模型（默认）还是云端模型服务商。

产品决策（2026-11-02）：**默认自部署**，只有显式 `MAT_ROUTE=cloud` 才走云端。

为什么默认值才是真决策：测试工程师装工具时想的是"能不能跑通"，不会去判断
"我的控件文本算不算敏感"；真正有隐私要求的（金融、政企、接甲方项目）也不会
主动翻文档找一个开关。**大多数人不表态 ⇒ 默认值就是事实标准。**

三种取值：

| `MAT_ROUTE` | 行为 |
|---|---|
| 未设 / `auto` | 按服务端策略：能自部署就自部署（free/pro/team），private 走云端 |
| `local` | **强制**自部署；端点缺失或不可用一律失败，**不静默改走云端** |
| `cloud` | 强制云端模型服务商（质量优先；文本与截图都会发给模型商） |

`local` 之所以要"失败而不回落"：选了它的人就是为了"内容别出去"，而回落恰好
发生在自部署不可用（Mac 睡眠、断网）这种最不该静默的时刻。宁可让这一次生成
失败并说清原因，也不能把截图悄悄发出去。
"""
from __future__ import annotations

import os

#: 环境变量名（工作区 `.env` 或进程环境；`.env` 由 `main()` / MCP / 控制台加载）。
ENV_KEY = "MAT_ROUTE"

AUTO = "auto"
LOCAL = "local"
CLOUD = "cloud"

#: 认得的取值。**不在这里的一律按 auto 处理**——拼错（`clod`）走的是安全方向
#: （自部署、不外发），并由 `mat doctor` 提示"这个值不认识"。
_KNOWN = {"": AUTO, "auto": AUTO, "local": LOCAL, "cloud": CLOUD}


def raw() -> str:
    """环境里的原始值（`doctor` 用来提示拼写错误）。"""
    return (os.environ.get(ENV_KEY) or "").strip().lower()


def preference() -> str:
    """规范化后的取值：`auto` / `local` / `cloud`。"""
    return _KNOWN.get(raw(), AUTO)


def is_cloud() -> bool:
    return preference() == CLOUD


def is_local() -> bool:
    return preference() == LOCAL


def is_explicit() -> bool:
    """用户是否**显式**表过态（未设 / auto 时为假，此时不带请求头）。"""
    return preference() != AUTO


def unknown_value() -> str:
    """设了但不认识的值（否则空串）——给 `doctor` 报出来。"""
    value = raw()
    return value if value and value not in _KNOWN else ""
