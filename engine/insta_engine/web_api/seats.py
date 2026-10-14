"""团队成员（商业化规则，发起人 2026-10-12 定；2026-10-14 改为服务端配额）。

本模块管的是**人数**：基线**按档位分**（标准版 5 人、免费体验版 2 人，见
`seat_quota.base_for_plan`），要更多人由运营按**主机**加配额（`seat_quota` +
`scripts/manage_seat.py`）。功能不分档全员一致，商业化只控**额度**与**人数**。本模块是"人数闸"的最小实现：

- 成员标识 = 触发者名字（首次进入登记；内网可信场景的轻量唯一性，与 job.operator
  同一语义——不做账号体系，那是 L4 重定义后的事）；
- 上限 = 免费基线 + 服务端按主机发的有效配额（取值规则收在 `seat_quota.effective_limit`）；
- 落工作区 `artifacts/team_seats.json`——--share 下全组共用同一份，随工作区备份。

free/pro 两种套餐行为完全一致（功能不分档）；额度控制已有（quota.py），互不掺和。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..seat_quota import BASE_SEATS, BASE_SEATS_FREE, BASE_SEATS_PRO, effective_limit

#: 免费基线。保留这个名字只为不打断既有引用；真值在 `seat_quota`（**按档位分**：
#: 标准版 5 人、免费体验版 2 人 —— 基线从"一个本地常量"变成了"基线 + 服务端配额"，
#: 取值规则也搬去了 `seat_quota.base_for_plan`）。
DEFAULT_SEAT_LIMIT = BASE_SEATS

#: 登记/核销的并发安全——「名额判断 + 落盘」必须原子，否则并发首次进入可超卖名额
_SEAT_LOCK = threading.Lock()


def seat_limit() -> int:
    """成员上限。取值规则**只有一处**：`seat_quota.effective_limit()`
    （免费基线 5 + 服务端按主机发的有效配额；没同步过时回落 `.env` 的 `MAT_SEAT_LIMIT`）。

    这里刻意只做薄包装：控制台各处（登记、写操作闸、设置页）都调它，
    规则集中在配额模块里，将来改口径不必到处找。
    """
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


def api_seats(root: Path) -> tuple[int, dict]:
    """GET /api/seats：成员名单 + 上限（设置页「团队成员」卡）。"""
    return 200, {"seats": read_seats(root), "limit": seat_limit()}


def _register_or_verify(root: Path, name: str) -> tuple[int, dict]:
    """登记/核销共用核心（api_seat_check 与 enforce 同源，避免两套口径漂移）：
    已在名单 → 刷新 last_seen 放行；新名字且未满 → 登记放行；已满 → 403。
    调用方须持 _SEAT_LOCK。"""
    seats = read_seats(root)
    for s in seats:
        if s.get("name") == name:
            s["last_seen"] = _now()
            _write_seats(root, seats)
            return 200, {"ok": True, "returning": True,
                         "seats": seats, "limit": seat_limit()}
    if len(seats) >= seat_limit():
        limit = seat_limit()
        return 403, {"error": f"团队成员已满（{len(seats)}/{limit}）——免费体验版 "
                              f"{BASE_SEATS_FREE} 人、标准版 {BASE_SEATS_PRO} 人。"
                              "要更多名额请联系客服开通（按主机标识登记，可分批、各有到期）",
                     "seats_full": True}
    seats.append({"name": name, "first_seen": _now(), "last_seen": _now()})
    _write_seats(root, seats)
    return 200, {"ok": True, "returning": False, "seats": seats, "limit": seat_limit()}


def api_seat_check(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/seats/check：首次进入登记/老成员核销（前端 OperatorPrompt 调用）。"""
    name = str((body or {}).get("name") or "").strip()[:40]
    if not name:
        return 400, {"error": "先填一个名字"}
    with _SEAT_LOCK:
        return _register_or_verify(root, name)


def enforce(root: Path, body: dict) -> tuple[int, dict] | None:
    """写操作人数闸（2026-10-12 补洞，发起人指出：此前「先匿名」或直接裸调 API
    可完全绕开名额——只在 seats/check 登记时校验，写操作一律不校验）。

    do_POST 对所有写端点先过闸：匿名 → 400（引导先设名字）；老名字 → 核销放行；
    新名字 → 自动登记（受上限约束，满 403 提示开通）。
    豁免：/api/seats/check（登记入口自身）、/api/activate（机器级授权——与成员名额无关，
    在我们给这台机器加名额之前，它也必须能激活）。返回 None = 放行，(code, payload) = 直接回客户端。"""
    name = str((body or {}).get("operator") or "").strip()[:40]
    if not name:
        return 400, {"error": "请先在右上角设置名字再操作——任务、审计与成员名额都按名字登记，"
                             "匿名不允许写操作", "need_operator": True}
    with _SEAT_LOCK:
        code, payload = _register_or_verify(root, name)
    return None if code == 200 else (code, payload)


def api_seat_remove(root: Path, body: dict) -> tuple[int, dict]:
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
    return 200, {"ok": True, "seats": seats, "limit": seat_limit()}
