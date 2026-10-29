"""本地配额与套餐状态（P0-1/P0-2 重构，2026-10-19）。

**额度口径（唯一事实源是 `policy/quota.json`，经 `quota_policy.py` 加载）**：

- **生成（gen）本地不再拦截**（P0-2）：官方代理模式下**服务端 429 是唯一权威**
  （旧实现客户端 50 次 vs 服务端 10 次/Pro"不限" vs 300，两边打架，实测伤付费信任）；
  BYOK 用户成本自付，本就不该限额。本地计数**只用于展示**（`mat status` / 控制台）。
- **视觉兜底（vision）只在官方代理下保留本地闸**：VL 调用有真实 token 成本，且失败优雅降级
  （退回定位失败，不中断执行）；额度耗尽按档位给可行动提示。
  **自带 Key（BYOK）不设额度**（`check_vision(enforce=False)`）：token 由用户自己的服务商
  计费，本地只计数供 `mat status` 展示——否则就是拿别人的钱、还替别人限额。
- **本地执行/自愈/报告**：不限量，无配额逻辑（不变承诺）。
- **计数按来源拆分**（2026-10-28）：`gen_count`/`vision_count` 仍是总数，
  另有 `gen_byok`/`vision_byok` 记录其中「自带 Key」的部分，官方用量 = 总数 − BYOK。
  拆分**只影响展示**，不参与任何拦截——目的是让用户看清「用自己的 Key 用了多少、
  官方额度有没有白买」，而不是把 BYOK 也纳入额度。

V1 为本地诚实计数；轻云上线后服务端配额为权威（license.json 校验随之升级，
见 README 的 open-core 边界——配额策略不放在开源仓库的信任边界内）。

⚠️ 档位名只是**显示名**：代码判据仍是 `plan`（pro/team/private），激活码仍是 `MAT-*.`
"""

from __future__ import annotations

import contextlib
import json
import warnings
from datetime import UTC, datetime
from pathlib import Path

from . import seat_quota
from .quota_policy import plan_limits as _policy_limits
from .state import state_home


def insta_home() -> Path:
    """用户状态目录；测试可用 INSTA_HOME / MAT_HOME 覆盖（优先级见 state_home）。"""
    return state_home()


class QuotaExceeded(RuntimeError):
    pass


def _vision_limit_for(plan: str) -> int | None:
    """视觉兜底月上限：pro 走服务端可配值（默认 100）；private 不限；其余按策略文件。"""
    if plan == "private":
        return None
    if plan == "pro":
        return seat_quota.vision_limit()
    return _policy_limits(plan)["vision"]


#: 总数 → BYOK 分项字段名（见 `UsageStore._bump`）。
_BYOK_FIELD = {"gen_count": "gen_byok", "vision_count": "vision_byok"}


def _current_source() -> str:
    """当前接入方式：`official`=走官方代理（平台出钱），`byok`=直连用户端点。

    判据与请求路径同源：`promptstore.is_server()` 为真时客户端打 `/mat/complete`，
    由平台转发并计费；否则直连用户自己的 `GEN_BASE_URL`，token 由用户结算。
    """
    try:
        from . import promptstore

        return "official" if promptstore.is_server() else "byok"
    except Exception:  # noqa: BLE001 - 配置异常按 BYOK 记，宁可少报官方用量
        return "byok"


class UsageStore:
    def __init__(self, path: Path | None = None, source: str | None = None):
        self.path = path or (insta_home() / "usage.json")
        #: 显式来源优先（"official"/"byok"）；不传则每次记账时按当前接入方式判定。
        self._source = source

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        month = datetime.now(UTC).strftime("%Y-%m")
        if data.get("month") != month:  # 月度滚动重置：两个计数一并归零
            data = {"month": month, "gen_count": 0, "vision_count": 0}
        data.setdefault("gen_count", 0)
        data.setdefault("vision_count", 0)  # 兼容 v0.4.0 之前只含 gen_count 的 usage.json
        # 来源拆分自 2026-10-28 起记录：缺失按 0 处理（升级当月之前的计数无法追溯，
        # 仍计入总数；usage.json 按月重置，下个月起自然准确）。
        data.setdefault("gen_byok", 0)
        data.setdefault("vision_byok", 0)
        return data

    def plan(self) -> str:
        """套餐判定：license.json 经 HMAC+过期校验（licensing，v0.4.0）。"""
        try:
            from .licensing import read_license
            return read_license().get("plan", "free")
        except Exception:
            return "free"

    def snapshot(self) -> dict:
        data = self._load()
        plan = self.plan()
        byok_gen = data.get("gen_byok") or 0
        byok_vision = data.get("vision_byok") or 0
        return {
            "month": data["month"],
            "plan": plan,
            "gen_count": data["gen_count"],
            # 来源拆分：official = 总数 − byok（只用于展示，见模块文档）
            "gen_byok": byok_gen,
            "gen_official": max(0.0, round(data["gen_count"] - byok_gen, 2)),
            # 生成**只统计展示，不再本地拦截**（P0-2）；limit 来自策略文件（private 为 None=不限）
            "gen_limit": _policy_limits(plan)["gen"],
            "gen_enforced": False,
            "vision_count": data["vision_count"],
            "vision_byok": byok_vision,
            "vision_official": max(0.0, round(data["vision_count"] - byok_vision, 2)),
            "vision_limit": _vision_limit_for(plan),
        }

    def ensure_capacity(self, amount: float = 1.0) -> dict:
        """【已废弃·不再拦截】生成额度预检。

        P0-2（2026-10-19）起**本地不再拦截生成**：官方代理由服务端 429 权威拒绝，
        BYOK 用户成本自付本就不该限。保留本方法只为兼容既有调用点（变为纯读快照）；
        新代码**不要**调用它做闸门。
        """
        return self.snapshot()

    def consume(self, amount: float = 1.0, byok: bool | None = None) -> dict:
        """生成用量核销（写入；成功落盘后调用，仅统计展示）。"""
        return self._bump("gen_count", amount, byok=self._is_byok(byok))

    def ensure_vision_capacity(self, amount: float = 1.0) -> dict:
        """视觉兜底额度预检（不写入）；额度不足抛 QuotaExceeded（真实 token 成本闸）。

        调用方（runner._vision_locate）捕获本异常后**优雅降级**为"vision 未启用"，
        不中断本地执行——vision 是兜底能力，额度耗尽只应退回定位失败。
        `private` 档（limit=None）本地不限；pro 的上限服务端可配（默认 100）。
        """
        data = self._load()
        limit = _vision_limit_for(self.plan())
        if limit is None:
            return self.snapshot()
        if data["vision_count"] + amount > limit:
            remaining = max(0.0, limit - data["vision_count"])
            plan = self.plan()
            if plan == "pro":
                raise QuotaExceeded(
                    f"本月视觉兜底额度（标准版 {limit:g} 次/月）已用完，次月自动恢复。"
                    "本次定位退回普通失败（等同未启用视觉兜底）；同屏缓存命中不计费，"
                    "如需更高额度请通过 mobileaitest.com 联系我们。"
                )
            raise QuotaExceeded(
                f"本月视觉兜底额度不足（剩 {remaining:g} 次，本次需 {amount:g} 次）。"
                "本次定位退回普通失败（等同未启用视觉兜底）；升级提高视觉兜底额度"
                "（档位见 mobileaitest.com）。"
            )
        return self.snapshot()

    def consume_vision(self, amount: float = 1.0, byok: bool | None = None) -> dict:
        """视觉兜底额度核销（写入；调用前应先 ensure_vision_capacity）。"""
        return self._bump("vision_count", amount, byok=self._is_byok(byok))

    def check_vision(self, amount: float = 1.0, enforce: bool = True) -> dict:
        """视觉兜底调用前：预检 + 计数原子完成（文件锁）。

        仅应在**真正调用模型前**触发——缓存命中零 token，不计费（见 vision.locate 的 before_call）。

        Args:
            enforce: `True`（默认）= 官方代理模式，额度不足抛 `QuotaExceeded`（平台出钱）。
                `False` = 自带 Key 模式，**只计数不预检**——成本由用户自己的服务商结算，
                平台没有理由设限；计数保留只为 `mat status` 展示真实用量。
        """
        def _op() -> dict:
            if enforce:
                self.ensure_vision_capacity(amount)
            # enforce=False 即自带 Key：成本由用户结算，只记 BYOK 分项
            return self.consume_vision(amount, byok=not enforce)
        return self._locked(_op)

    # -- 内部 ---------------------------------------------------------------

    def _is_byok(self, flag: bool | None) -> bool:
        """本次记账是否算 BYOK：显式 flag > 构造时来源 > 当前接入方式。"""
        if flag is not None:
            return flag
        source = self._source or _current_source()
        return source != "official"

    def _bump(self, field: str, amount: float, byok: bool = False) -> dict:
        """读-改-写计数（写盘尽力而为）。

        写失败（HOME 只读/沙箱拦截等异常环境）不抛——计数只是展示辅助，把辅助计数
        升级成整个调用失败，代价是用户真实工作被打断（2026-10-13 实测：沙箱化服务
        进程写不了 ~/.mat ⇒ 视觉兜底整链 PermissionError 失败）。
        """
        data = self._load()
        data[field] = round(data[field] + amount, 2)
        if byok:
            key = _BYOK_FIELD[field]
            data[key] = round((data.get(key) or 0) + amount, 2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            warnings.warn(f"配额计数写盘失败（{self.path}），本次调用按未计数继续",
                          RuntimeWarning, stacklevel=2)
        return self.snapshot()

    def _locked(self, fn):
        """文件锁包裹"读-改-写"（POSIX；无 fcntl 的平台降级为无锁）。"""
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_fh = open(lock_path, "w", encoding="utf-8")
        except OSError as exc:
            # 锁打不开 ≠ 额度不足——降级为无锁继续，预检/核销照走（写失败由
            # _bump 再降级），不让辅助计数阻断用户工作（同上实测）。
            warnings.warn(f"配额锁不可用（{exc}），降级为无锁计数",
                          RuntimeWarning, stacklevel=2)
            return fn()
        with lock_fh:
            try:
                import fcntl
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            except ImportError:
                # 无 fcntl 的平台（Windows）静默降级为无锁 ⇒ 并发下配额计数可能竞态。
                # 本地计数仅展示，可接受，但降级不该无声（评审 L15）。
                warnings.warn(
                    "当前平台无文件锁（fcntl 不可用），配额计数在并发下可能竞态",
                    RuntimeWarning, stacklevel=2)
            try:
                return fn()
            finally:
                with contextlib.suppress(Exception):
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
