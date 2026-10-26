"""配额策略唯一事实源（P0-1，2026-10-19）。

**为什么存在**：此前客户端（`quota.py`）与服务端（`cloud/matcloud/config.py`）各自
硬编码额度，实测漂移成「客户端说免费 50 次、服务端只给 10 次；客户端说 Pro 不限、
服务端 300 次就拒」——这是付费信任 bug。现在两边都读同一份 `policy/quota.json`
（随各包分发 `data/quota.json` 副本，`scripts/check_policy_parity.py` 盯漂移）。

**口径（与实现一致，2026-10-19 起）**：

- **生成（gen）不再本地拦截**：官方代理模式下服务端 429 是唯一权威；BYOK 用户
  成本自付，本就不该限额。本地计数仅用于展示。
- **视觉兜底（vision）仍有本地闸**：VL 调用有真实 token 成本，且失败优雅降级
  （退回定位失败，不中断执行）。`private` 档为 `null` = 本地不限（离线交付，
  模型端点由客户自管）。
- **取值顺序**：`MAT_QUOTA_POLICY`（路径或内联 JSON）→ 包内 `data/quota.json`
  → 内置默认（与 JSON 同值，防御文件缺失）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: 内置默认——**必须与 policy/quota.json 逐值一致**（一致性测试盯着）。
_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "reset": "monthly_utc",
    "plans": {
        "free": {"gen": 60, "vision": 20},
        "pro": {"gen": 2000, "vision": 300},
        "team": {"gen": 10000, "vision": 1000},
        "private": {"gen": None, "vision": None},
    },
}


def _packaged_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "quota.json"


def load_policy() -> dict[str, Any]:
    """读策略：env 覆盖 → 包内副本 → 内置默认。坏文件一律静默降级，不让配额崩功能。"""
    raw = os.environ.get("MAT_QUOTA_POLICY", "").strip()
    if raw:
        try:
            text = Path(raw).read_text(encoding="utf-8") if Path(raw).exists() else raw
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("plans"), dict):
                return data
        except (OSError, ValueError):
            pass
    try:
        data = json.loads(_packaged_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("plans"), dict):
            return data
    except (OSError, ValueError):
        pass
    return json.loads(json.dumps(_DEFAULTS))  # deep copy，防调用方改到默认值


def plan_limits(plan: str) -> dict[str, int | None]:
    """档位额度：`{"gen": int|None, "vision": int|None}`；未知档位按 free（宁低勿高）。"""
    plans = load_policy()["plans"]
    entry = plans.get(plan) or plans.get("free") or {}
    gen, vision = entry.get("gen"), entry.get("vision")
    return {
        "gen": int(gen) if gen is not None else None,
        "vision": int(vision) if vision is not None else None,
    }
