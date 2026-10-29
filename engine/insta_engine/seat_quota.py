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

## 团队共享只随 team/private 授权（2026-10-25 产品定位调整）

发起人 2026-10-25 定：**标准/体验授权面向单人使用**——`--share`（共享控制台）下
成员上限恒为 1，第二位同事拿到的是「团队共享随团队授权提供」的可行动提示
（闸门在 `web_api/seats.seat_limit`，判定用下面的 `team_authorized`）。

此前是「免费 2 人 / 标准 5 人，用基线当升级触发器」（2026-10-14）——**已作废**：
那样 `--share` 对每个档位都开放，"团队模式"一词于是有两个含义（授权档 vs
`--share`），`mat status` 与控制台各说各话。现在只有一个含义：**团队授权**。

⚠️ 与成员名额同哲学：这是**本地规则的自我约束**，不是 DRM（改 `.env` 的
`MAT_SEAT_LIMIT`、删 `artifacts/team_seats.json`、改源码都能绕）。对外口径说
「团队共享随团队授权提供」，不说「标准版锁死单人」。

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

#: 成员名额的**档位基线**（2026-10-14 发起人定）：标准/团队 5 人、免费体验版 2 人、
#: 私有化 10 人。
#: ⚠️ 2026-10-25 起 free/pro 的基线**不开放共享**（产品规则见 `team_authorized` 与
#: `web_api/seats.seat_limit` 闸门）：这两个值只剩算术与测试意义，别再当"升级触发器"讲
#: ——那个设计（免费 2 人试协作、第 3 人升级）已随本次调整作废。
BASE_SEATS_PRO = 5
BASE_SEATS_FREE = 2
#: Private 档基线（P0-3）：合同默认 10 席，license v2 的 seats 字段可覆盖
BASE_SEATS_PRIVATE = 10
#: 兼容既有引用（服务端 `config.SEAT_BASE` 下发的是同一个值）。
BASE_SEATS = BASE_SEATS_PRO

#: 视觉兜底（标准版）月上限默认值：服务端未下发 `vision_base` 时用它
#: （发起人 2026-10-14 定 300 并要求做成服务端可配置——与成员名额同一套主机配额机制）。
#: ⚠️ 2026-10-24 评审 P1-5：**服务端的 vision 字段至今未实现**——`/seat/status`
#: 不下发 `vision_base`，`seat_grants` 表也没有 vision 列。下面这套读取逻辑是为
#: 该规划预留的**前向兼容接收端**（测试用合成缓存钉住行为）；当前实际效果是
#: 「同步过 ⇒ 回落默认 100」，`.env` 的 `MAT_VISION_LIMIT` 只在**从未同步**时生效。
#: 宣称按此口径讲，别对外说"服务端可配"。
DEFAULT_VISION_LIMIT = 100


def vision_limit(now: int | None = None) -> int:
    """本机视觉兜底（标准版）月上限——与成员名额共用一份主机配额缓存。

    取值顺序：

    1. **同步过**（缓存里有 `grants` 或 `vision_base` 键）⇒ 以服务端为准：
       `vision_base` 与**当前有效批次**的 `vision` 取**最大**——月上限是限制
       不是福利，多批并存（如过渡期临时加倍）取宽的那个，批次过期自动回落。
       ⚠️ 服务端 vision 字段未上线（见 DEFAULT_VISION_LIMIT 的注释）——此分支
       当前实际回落默认 100，读取逻辑保留给规划中的服务端下发；
    2. **从没同步过** ⇒ 回落 `.env` 的 `MAT_VISION_LIMIT`（过渡期的人工口子），
       再否则默认 100。
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


def licensed_seats() -> int | None:
    """license v2 里签明的席位数（Team/Private 合同口径）；无 v2 或坏档返回 None。

    合同席位**覆盖**档位基线（客户买的就是这个数）；服务端按主机加发的 extra
    照旧叠加（运营临时扩容不受影响）。
    """
    from .licensing import read_license

    try:
        seats = read_license().get("seats")
    except Exception:
        return None
    try:
        n = int(seats) if seats is not None else None
    except (TypeError, ValueError):
        return None
    return n if n and n >= 1 else None


def team_authorized() -> bool:
    """本机授权是否开放**团队共享**（`--share` 下的成员名额，2026-10-25 起的唯一入口）。

    产品规则（发起人 2026-10-25 定）：**标准/体验授权面向单人使用**——只有
    team/private 授权、或 license v2 明写的合同席位，才开放共享；其余档位在
    `--share` 下成员上限恒为 1，第二位同事由 `web_api/seats._full_message`
    给「团队共享随团队授权提供」的可行动提示。

    v2 的 `seats` **优先于 plan 判定**：合同里签了席位就是买了协作，不管那本书
    签的是 pro 还是 team（`scripts/issue_license_v2.py --plan pro --seats 7`
    是合法组合）。读不到授权/坏档一律按未激活处理（fail-closed 到单人）。

    ⚠️ 本地规则、不是 DRM（与成员名额同哲学）：改 `.env`、删名单、改源码都能绕。
    对外说「团队共享随团队授权提供」，不说「标准版锁死」。
    """
    from .licensing import read_license

    try:
        lic = read_license()
    except Exception:  # noqa: BLE001 —— 读不到授权按未激活（落到单人）
        return False
    plan = str(lic.get("plan") or "free").lower()
    if plan in ("team", "private"):
        return True
    try:
        n = int(lic.get("seats")) if lic.get("seats") is not None else None
    except (TypeError, ValueError):
        n = None
    return bool(n and n >= 1)


def base_for_plan(plan: str | None = None) -> int:
    """按档位取成员基线：pro/team 5 人、private 10 人，其余（未激活 / 已过期）⇒ 2 人。

    ⚠️ **纯算术层**：2026-10-25 起 free/pro 的基线不开放共享（产品规则在
    `team_authorized()` + `web_api/seats.seat_limit()` 闸门），这两个值只在
    team/private 路径与测试算术里有意义——别拿它当"免费能拉 2 个人"讲。

    ⚠️ 基线由**客户端按本机 plan 算**，不采信服务端下发的 `base`：服务端按**主机**查配额时
    并不知道这台机器有没有有效授权 —— 要它知道就得让客户端上报 plan，那反而多一个伪造面。
    基线是**产品规则**（免费能试用、标准版更多），放本地算更简单；服务端下发的 `extra`
    （运营按人加的名额）职责不重叠，照旧叠加。
    """
    if plan is None:
        from .licensing import read_license

        plan = str(read_license().get("plan") or "free")
    return {"pro": BASE_SEATS_PRO, "team": BASE_SEATS_PRO,
            "private": BASE_SEATS_PRIVATE}.get(plan, BASE_SEATS_FREE)


def effective_limit(now: int | None = None) -> int:
    """本机成员上限 = **档位基线** + 有效配额。

    取值顺序：

    1. **同步过**（缓存里有 `grants` 键，哪怕是空数组）⇒ `基线 + 有效配额`
       —— 空数组是有意义的答案（"这个客户没有任何额外名额"），不能当成"没配置"。
    2. **从没同步过** ⇒ 回落 `.env` 的 `MAT_SEAT_LIMIT`（过渡期的人工口子），再否则基线。
    """
    base = licensed_seats() or base_for_plan()
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
    if not team_authorized():
        # 2026-10-25：未授权（体验/标准/失效）不开放共享——别把档位算术里的
        # "免费基线 2 人"打印给用户看，那台机器 `--share` 下也只有 1 人。
        return ("成员上限 1 人（本机单人）：当前授权面向单人使用，团队共享随团队授权提供"
                "——关注公众号「Mobile AI Test」留言你的场景，或加微信 lazy_vizen")
    cache = read_cache()
    if not cache:
        return (f"成员上限 {effective_limit(now)} 人（档位基线；尚未同步过服务端配额，"
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
