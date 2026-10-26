"""团队成员（商业化规则，发起人 2026-10-12 定；2026-10-14 改为服务端配额）。

本模块管的是**人数**，而"是否有别人"取决于控制台有没有共享出去（2026-10-23），
以及**授权开不开放共享**（2026-10-25）：

- **本机模式（默认，`mat console`，只听 127.0.0.1）**：上限恒为 **1**——只有安装这台
  机器的你。此时谈"5 个成员"没有意义，控制台物理上只有本机能打开。
- **共享模式（`mat console --share`，听 0.0.0.0 + 访问口令）**：**只有 team/private
  授权**（或 v2 合同席位，见 `seat_quota.team_authorized`）才给成员名额——标准/体验
  授权**面向单人使用**，`--share` 下也恒为 1，第二位同事拿到「团队共享随团队授权提供」
  的可行动提示。要更多人由运营按**主机**加配额（`seat_quota` + `scripts/manage_seat.py`）。

⚠️ "团队模式"这个词在本仓库**只指授权档**（team/private）；`--share` 叫**共享模式**。
2026-10-25 之前两者混用（`mat status` 按授权显示、控制台按 `--share` 显示），
同一个用户会同时看到"标准模式"和"团队模式"——已统一。

成员标识 = 触发者名字（首次进入登记；内网可信场景的轻量唯一性，与 job.operator 同一语义
——不做账号体系）；名单落工作区 `artifacts/team_seats.json`，共享模式下全组共用同一份。

功能不分档全员一致，商业化只控**额度**与**人数**；额度见 `quota.py`，互不掺和。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..seat_quota import BASE_SEATS, effective_limit, team_authorized

#: 免费基线。保留这个名字只为不打断既有引用；真值在 `seat_quota`（**按档位分**：
#: 标准/团队 5 人、免费 2 人、私有化 10 人 —— 基线从"一个本地常量"变成了"基线 + 服务端配额"，
#: 取值规则也搬去了 `seat_quota.base_for_plan`；而**要不要开放共享**看 `team_authorized`）。
DEFAULT_SEAT_LIMIT = BASE_SEATS

#: 登记/核销的并发安全——「名额判断 + 落盘」必须原子，否则并发首次进入可超卖名额
_SEAT_LOCK = threading.Lock()


#: 本机模式（未 `--share`）的成员上限：只有安装者本人。
SINGLE_SEAT_LIMIT = 1


def seat_limit(shared: bool = False) -> int:
    """成员上限。

    - `shared=False`（默认，本机监听）⇒ **1**：没有第二个人能打开这个控制台，
      额度/名单都不该出现第二个名字。
    - `shared=True`（`--share` 的共享模式）⇒ **只有团队授权**才按
      `seat_quota.effective_limit()` 给名额（2026-10-25：标准/体验授权面向单人使用，
      `--share` 下也恒 1）；未授权时第二位同事由 `_full_message` 给升级指向。

    调用方负责如实传入"当前是否共享"（`web_server` 按监听地址判断），
    这样不用全局状态、测试也能分别锁两种模式。
    """
    if not shared:
        return SINGLE_SEAT_LIMIT
    if not team_authorized():
        return SINGLE_SEAT_LIMIT
    return effective_limit()


def _seats_path(root: Path) -> Path:
    return Path(root) / "artifacts" / "team_seats.json"


def read_seats(root: Path) -> list[dict]:
    """成员名单 [{name, first_seen, last_seen}]；文件缺失/损坏视为空。"""
    try:
        data = json.loads(_seats_path(root).read_text(encoding="utf-8"))
        return data.get("seats", []) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_seats(root: Path, seats: list[dict]) -> None:
    p = _seats_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"seats": seats}, ensure_ascii=False, indent=1),
                 encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def api_seats(root: Path, shared: bool = False) -> tuple[int, dict]:
    """GET /api/seats：成员名单 + 上限（设置页「团队成员」卡）。

    `shared` 与 `team_authorized` 一并回给前端：前者区分「本机单人」与「共享模式」，
    后者区分「共享了但没授权」（提示升级）与「团队授权」（正常成员管理）——
    不能拿 limit 推授权状态：团队合同席位可以是 1（`issue_license_v2 --seats 1`）。
    """
    return 200, _seats_payload(root, read_seats(root), shared)


def _seats_payload(root: Path, seats: list[dict], shared: bool, **extra) -> dict:
    """名单响应的统一形状（四个端点共用，避免各写一遍漏了 team_authorized）。"""
    out = {"seats": seats, "limit": seat_limit(shared), "shared": shared,
           "team_authorized": team_authorized()}
    out.update(extra)
    return out


def _unauthorized_plan_label() -> str:
    """未授权共享时告诉用户"你当前是哪档"——体验/标准（失效授权按体验）。"""
    from ..licensing import read_license

    try:
        plan = str(read_license().get("plan") or "free").lower()
    except Exception:  # noqa: BLE001 —— 读不到按未激活
        plan = "free"
    return "标准模式" if plan == "pro" else "体验模式"


def _full_message(shared: bool, used: int, limit: int) -> str:
    """名额满的可行动提示——三种处境给三个不同的下一步，别让人去查"档位"。"""
    if not shared:
        return (f"当前是本机单人模式（{used}/{limit}）：只有安装这台机器的你。"
                "要和同事共用同一个控制台，请用 `mat console --share` 启动——"
                "团队共享随团队授权提供（标准/体验授权面向单人使用）")
    if not team_authorized():
        return (f"团队共享随团队授权提供：当前是{_unauthorized_plan_label()}授权，"
                "面向单人使用——`--share` 下名额只有 1 个，已被名单里的名字占用。"
                "如果那就是你自己（换了浏览器/设备，或改了名字）：到「设置 → 成员管理」"
                "移除旧名字即可进入；要和同事共用同一个控制台，需要团队授权："
                "关注公众号「Mobile AI Test」留言你的场景，或加微信 lazy_vizen")
    return (f"团队成员已满（{used}/{limit}）——要更多名额请联系我们"
            "（按主机标识登记，可分批、各有到期）")


def _register_or_verify(root: Path, name: str, shared: bool = False) -> tuple[int, dict]:
    """登记/核销共用核心（api_seat_check 与 enforce 同源，避免两套口径漂移）：
    已在名单 → 刷新 last_seen 放行；新名字且未满 → 登记放行；已满 → 403。
    调用方须持 _SEAT_LOCK。"""
    seats = read_seats(root)
    for s in seats:
        if s.get("name") == name:
            s["last_seen"] = _now()
            _write_seats(root, seats)
            return 200, _seats_payload(root, seats, shared, ok=True, returning=True)
    limit = seat_limit(shared)
    if len(seats) >= limit:
        return 403, {"error": _full_message(shared, len(seats), limit),
                     "seats_full": True, "shared": shared}
    seats.append({"name": name, "first_seen": _now(), "last_seen": _now()})
    _write_seats(root, seats)
    return 200, _seats_payload(root, seats, shared, ok=True, returning=False)


def api_seat_check(root: Path, body: dict, shared: bool = False) -> tuple[int, dict]:
    """POST /api/seats/check：首次进入登记/老成员核销（前端 OperatorPrompt 调用）。

    方案 B（2026-10-25 第三处收口）：单人模式名字只是**本地标签**——不登记、
    不校验名额。否则首次设名占用 1/1 席位后，改名（同端点换新名字）会被
    「成员已满」拒（用户实测：设名后无法改名）；共享模式照旧登记+限额。
    """
    name = str((body or {}).get("name") or "").strip()[:40]
    if not name:
        return 400, {"error": "先填一个名字"}
    if not shared:
        return 200, _seats_payload(root, read_seats(root), shared, ok=True, returning=False)
    with _SEAT_LOCK:
        return _register_or_verify(root, name, shared)


def enforce(root: Path, body: dict, shared: bool = False) -> tuple[int, dict] | None:
    """写操作人数闸（2026-10-12 补洞，发起人指出：此前「先匿名」或直接裸调 API
    可完全绕开名额——只在 seats/check 登记时校验，写操作一律不校验）。

    do_POST 对所有写端点先过闸。**仅团队授权下的共享模式**要求实名：匿名 → 400
    （引导先设名字）；老名字 → 核销放行；新名字 → 自动登记（受上限约束，满 403
    提示开通）。单人模式与未授权共享免名（后者本质也是单人，见下方注释）。
    豁免：/api/seats/check（登记入口自身）、/api/activate（机器级授权——与成员名额无关，
    在我们给这台机器加名额之前，它也必须能激活）。返回 None = 放行，(code, payload) = 直接回客户端。"""
    # 2026-10-25 方案 B（发起人批准）：**单人模式（未 --share）免名**——写操作直接
    # 放行，operator 由 `operator_of` 兜底为「本机」；不登记席位、不做名额校验。
    # 动机：产品定位「插手机就能测」，单人首次跑用例被弹窗问名字是反定位的摩擦；
    # 且单人席位 1/1 被首个名字绑定后，换浏览器/清缓存输新名字会被「成员已满」拒
    # （实测踩过），纯支持负担。
    # 🔴 未授权共享（--share 但无团队授权）同族（4d82e64 锁死面后半段）：名额同样
    # 恒 1，名单里那个名字多半就是安装者自己（换浏览器/清 localStorage 后 operator
    # 为空）。若仍要求实名 ⇒ 设名被 403 拒（名额满）、移除被 400 拒（需实名），
    # 「设置 → 成员管理移除旧名字即可进入」那条出路成死路。**团队授权下共享照旧
    # 实名 + 限额**（真团队场景的安全语义不变）。
    if not shared or not team_authorized():
        return None
    name = str((body or {}).get("operator") or "").strip()[:40]
    if not name:
        return 400, {"error": "请先在右上角设置名字再操作——任务、审计与成员名额都按名字登记，"
                             "匿名不允许写操作", "need_operator": True}
    with _SEAT_LOCK:
        code, payload = _register_or_verify(root, name, shared)
    return None if code == 200 else (code, payload)


def api_seat_remove(root: Path, body: dict, shared: bool = False) -> tuple[int, dict]:
    """POST /api/seats/remove：移除成员（误填/离职）。

    🔴 **不校验调用者身份**（2026-10-14 实测确认）：任何**已登记**的成员都能移除任何人，
    包括移除自己、把名单清空。唯一门槛是 `enforce` 要求带一个已登记的名字（匿名 400）。
    为什么不加"只有管理员能删"：这套机制是**内网可信场景的轻量唯一性**（与 job.operator
    同语义），不是账号体系（那是 L4 的事）；而且改 `artifacts/team_seats.json`、走 CLI
    都能绕，加权限只挡手滑、挡不住有意——而"手滑填错的名字"恰恰需要别人帮忙清。
    缓解：**全部落审计**（`artifacts/audit.jsonl` 记 operator/target/IP），能查不能拦。
    """
    name = str((body or {}).get("name") or "").strip()
    seats = [s for s in read_seats(root) if s.get("name") != name]
    _write_seats(root, seats)
    return 200, _seats_payload(root, seats, shared, ok=True)
