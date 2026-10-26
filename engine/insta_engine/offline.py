"""离线模式（P0-4，2026-10-19）。

**为什么要有它**：产品承诺「数据不出本机」，但这句承诺此前只靠"代码恰好没在
run 路径调模型"维持，没有任何机制兜底。`MAT_OFFLINE=1` 把承诺变成可执行开关：

- 需要联网的动作（AI 生成/归因、激活登记、邀请、版本检查、webhook 通知）
  **在发起网络请求前**就被拒绝，给出可行动提示，而不是等 10s 超时；
- 本地执行/回放/自愈/证据报告**完全不受影响**——这正是产品的核心闭环；
- 私有化交付包（P2-3）默认写入 `MAT_OFFLINE=1`。

判定只看环境变量 `MAT_OFFLINE`（`1/true/yes/on` 均开）。零网络测试见
`tests/test_offline_guarantee.py`（socket 级断言 `mat run` 零出网）。
"""

from __future__ import annotations

import os


class OfflineError(RuntimeError):
    """离线模式下拒绝了需要联网的操作；消息直接给用户看，要可行动。"""


_TRUTHY = {"1", "true", "yes", "on"}


def is_offline() -> bool:
    """是否处于离线模式（`MAT_OFFLINE` ∈ {1,true,yes,on}）。"""
    return os.environ.get("MAT_OFFLINE", "").strip().lower() in _TRUTHY


def require_online(action: str) -> None:
    """离线模式下抛 OfflineError；调用方把它当"明确的业务失败"处理，不要重试。"""
    if is_offline():
        raise OfflineError(
            f"离线模式（MAT_OFFLINE=1）：{action} 需要联网，已按离线策略拒绝。"
            "本地执行/回放/自愈/报告不受影响；要恢复联网能力请移除 MAT_OFFLINE 或设为 0。")
