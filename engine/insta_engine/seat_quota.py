"""团队成员名额（原称「席位」）的**服务端配额**（2026-10-14 起，取代"写进激活码"那套）。

## 为什么名额不放激活码里

成员名额（人数）与授权（有效期）是两个**独立维度**：客户完全可能"授权到 2027-01-31，
但只买 3 个人、为期 2 个月"。塞进同一张码就得为"加名额"重新签码，而且两个维度的到期日
会互相牵制（实测踩过：加名额被判成"这张码不必再用"，名额永远加不上）。

现在分家：**授权归激活码、成员名额归服务端按主机发的配额**，各管各的到期。

## 数据流

1. 客户跑 `mat seat id` 拿到本机标识，发给运营；
2. 运营跑 `scripts/manage_seat.py grant <标识> --seats 3 --until 2026-12-30`；
3. 客户那边 `mat seat sync`（或打开控制台时自动同步一次）拉下来，落本地缓存。

识别单位是**这台主机**（`machine_id`）：成员名额本来就是"这一台跑控制台的主机上有几个人"，
而且它是客户端现成的、天然的、**没法冒用**（报别人的标识也没用，配额发给的是那台机器）。

## 缓存为什么存**明细**而不是一个总数

因为**到期回落必须在离线时也成立**：缓存里存各批 `(seats, expires_at)`，本地按当前时间
算有效合计 ⇒ 2026-12-30 那天客户即使断网，也不会继续享有那 3 个名额。

## 基线按档位分（2026-10-14）

**标准版 5 人、免费体验版 2 人**（见 `base_for_plan`）。免费档的 2 人是刻意的**升级触发器**：
`--share` 是多人一起用的唯一入口，2 个人足以验证"协作是不是我要的"，第 3 个人就得升级。
（所以"锁 `--share`"是**不需要**的——把基线压到 2 就同时做到了"能试"和"要升级"。）

⚠️ 成员名额终究是**本地规则的自我约束**（控制台拦人），不是防破解：客户改 `.env` 的
`MAT_SEAT_LIMIT` 或删 `artifacts/team_seats.json` 都能绕开。它的价值是"给客户一条官方
途径 + 让运营可控"，**不要对外承诺这是 DRM**。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .licensing import insta_home, machine_id

#: 成员名额的**档位基线**（2026-10-14 发起人定）：标准版 5 人、免费体验版 2 人。
#: 免费档给 2 人不是"抠"——`--share` 是多人一起用的**唯一入口**，2 个人足够验证
#: "协作是不是我要的"，第 3 个人就得升级：这是**刻意的升级触发器**（见 MEMORY 商业模式条目）。
BASE_SEATS_PRO = 5
BASE_SEATS_FREE = 2
#: 兼容既有引用（服务端 `config.SEAT_BASE` 下发的是同一个值）。
BASE_SEATS = BASE_SEATS_PRO

#: 视觉兜底（标准版）月上限默认值：服务端未下发 `vision_base` 时用它
#: （发起人 2026-10-14 定 300 并要求做成服务端可配置——与成员名额同一套主机配额机制）
DEFAULT_VISION_LIMIT = 300


def vision_limit(now: int | None = None) -> int:
    """本机视觉兜底（标准版）月上限——与成员名额共用一份主机配额缓存。

    取值顺序：

    1. **同步过**（缓存里有 `grants` 或 `vision_base` 键）⇒ 以服务端为准：
       `vision_base` 与**当前有效批次**的 `vision` 取**最大**——月上限是限制
       不是福利，多批并存（如过渡期临时加倍）取宽的那个，批次过期自动回落；
    2. **从没同步过** ⇒ 回落 `.env` 的 `MAT_VISION_LIMIT`（过渡期的人工口子），
       再否则默认 300。
    """
    cache = read_cache()
    now = int(now if now is not None else time.time())
    if cache.get("grants") is not None or cache.get("vision_base") is not None:
        vals = [int(cache.get("vision_base") or DEFAULT_VISION_LIMIT)]
        for grant in cache.get("grants") or []:
            if not isinstance(grant, dict):
                continue
            try:
                if int(grant.get("expires_at") or 0) > now and grant.get("vision"):
                    vals.append(int(grant["vision"]))
            except (TypeError, ValueError):
                continue
        return max(1, max(vals))
    raw = os.environ.get("MAT_VISION_LIMIT", "")
    try:
        n = int(raw)
        return n if n >= 1 else DEFAULT_VISION_LIMIT
    except ValueError:
        return DEFAULT_VISION_LIMIT


def cache_path() -> Path:
    return insta_home() / "seat.json"


def read_cache() -> dict:
    """读成员配额缓存；文件不存在/损坏一律返回 `{}`（调用方据此区分"没同步过"）。"""
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(payload: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def active_extra_seats(now: int | None = None) -> int:
    """缓存里**当前仍然有效**的额外名额合计（不含免费基线）。

    ⚠️ 按 `expires_at` 现场算，所以**过期即失效**——不需要任何定时任务，也不需要
    客户联网（断网时用同一份明细照样算得对）。
    单条脏记录跳过，不让它把整个配额算崩。
    """
    now = int(now if now is not None else time.time())
    total = 0
    for grant in read_cache().get("grants") or []:
        if not isinstance(grant, dict):
            continue
        try:
            if int(grant.get("expires_at") or 0) > now:
                total += max(0, int(grant.get("seats") or 0))
        except (TypeError, ValueError):
            continue
    return total


def base_for_plan(plan: str | None = None) -> int:
    """按档位取成员基线：`pro` ⇒ 5 人；其余（未激活 / 已过期）⇒ 2 人。

    ⚠️ 基线由**客户端按本机 plan 算**，不采信服务端下发的 `base`：服务端按**主机**查配额时
    并不知道这台机器有没有有效授权 —— 要它知道就得让客户端上报 plan，那反而多一个伪造面。
    基线是**产品规则**（免费能试用、标准版更多），放本地算更简单；服务端下发的 `extra`
    （运营按人加的名额）职责不重叠，照旧叠加。
    """
    if plan is None:
        from .licensing import read_license

        plan = str(read_license().get("plan") or "free")
    return BASE_SEATS_PRO if plan == "pro" else BASE_SEATS_FREE


def effective_limit(now: int | None = None) -> int:
    """本机成员上限 = **档位基线** + 有效配额。

    取值顺序：

    1. **同步过**（缓存里有 `grants` 键，哪怕是空数组）⇒ `基线 + 有效配额`
       —— 空数组是有意义的答案（"这个客户没有任何额外名额"），不能当成"没配置"。
    2. **从没同步过** ⇒ 回落 `.env` 的 `MAT_SEAT_LIMIT`（过渡期的人工口子），再否则基线。
    """
    base = base_for_plan()
    cache = read_cache()
    if cache.get("grants") is not None:
        return max(1, base + active_extra_seats(now))
    raw = os.environ.get("MAT_SEAT_LIMIT", "")
    try:
        n = int(raw)
        return n if n >= 1 else base
    except ValueError:
        return base


def sync_from_server() -> dict:
    """联网拉取本机的成员配额并落缓存。

    ⚠️ 调用方必须容忍 `CloudError`（断网是常态）—— 拉不到就继续用缓存，
    缓存里没有配额就回落免费基线。名额**不该因为断网把客户挡在门外**。
    """
    from .cloudapi import get

    mid = machine_id()
    out = get(f"/seat/status?machine_id={mid}")
    write_cache({"machine_id": mid, "synced_at": int(time.time()),
                 # `base` 仅作留档（服务端按主机查，它不知道本机档位）——成员上限的基线由
                 # `base_for_plan()` 按本机 plan 算，见该函数。
                 "base": int(out.get("base") or BASE_SEATS),
                 "vision_base": int(out.get("vision_base") or DEFAULT_VISION_LIMIT),
                 "grants": out.get("grants") or []})
    return out


def describe(now: int | None = None) -> str:
    """给 `mat seat status` 用的一行说明（含各批的到期时间）。"""
    now = int(now if now is not None else time.time())
    cache = read_cache()
    if not cache:
        return (f"成员上限 {effective_limit(now)} 人（免费基线；尚未同步过服务端配额，"
                "跑 `mat seat sync` 可拉取）")
    lines = [f"成员上限 {effective_limit(now)} 人"
             f"（基线 {base_for_plan()} + 额外 {active_extra_seats(now)}）"]
    for grant in cache.get("grants") or []:
        try:
            exp = int(grant.get("expires_at") or 0)
            mark = "有效" if exp > now else "已过期"
            when = time.strftime("%Y-%m-%d", time.localtime(exp))
            note = str(grant.get("note") or "").strip()
            lines.append(f"  · {int(grant.get('seats') or 0)} 人，到 {when}（{mark}）"
                         + (f" —— {note}" if note else ""))
        except (TypeError, ValueError):
            lines.append("  · （有一条配额记录读不出来，已跳过）")
    return "\n".join(lines)
