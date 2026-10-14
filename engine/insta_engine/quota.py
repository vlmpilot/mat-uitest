"""本地配额与套餐状态。

体验版：NL 生成 50 次/月；视觉兜底 20 次/月；
本地执行/自愈/报告不限量（无配额逻辑）。
V1 为本地诚实计数；轻云上线后切换为服务端配额 + 签名授权（license.json 校验随之升级，
见 README 的 open-core 边界——配额策略不放在开源仓库的信任边界内）。

⚠️ 档位名只是**显示名**：代码判据仍是 `plan == "pro"`、激活码仍是 `MAT-pro.*`。
"""

from __future__ import annotations

import contextlib
import json
import warnings
from datetime import UTC, datetime
from pathlib import Path

from . import seat_quota
from .state import state_home

FREE_GEN_LIMIT = 50  # 体验版 AI 生成月上限（发起人 2026-10-14 定：50/月，取消首月欢迎额度双轨）
FREE_VISION_LIMIT = 20  # 视觉兜底（vision-llm）体验版月上限（发起人 2026-10-14 定：20/月）
#: 标准版视觉兜底月上限的**默认值**（发起人 2026-10-14 定 300；2026-10-14 起做成
#: 服务端可配置——真实取值走 `seat_quota.vision_limit()`：服务端按主机下发
#: `vision_base`，没同步过时回落 `.env` 的 `MAT_VISION_LIMIT`，再否则本默认值）
PRO_VISION_LIMIT = seat_quota.DEFAULT_VISION_LIMIT


def insta_home() -> Path:
    """用户状态目录；测试可用 INSTA_HOME / MAT_HOME 覆盖（优先级见 state_home）。"""
    return state_home()


class QuotaExceeded(RuntimeError):
    pass


class UsageStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (insta_home() / "usage.json")

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
        pro = plan == "pro"
        return {
            "month": data["month"],
            "plan": plan,
            "gen_count": data["gen_count"],
            "gen_limit": None if pro else FREE_GEN_LIMIT,
            "vision_count": data["vision_count"],
            "vision_limit": seat_quota.vision_limit() if pro else FREE_VISION_LIMIT,
        }

    def ensure_capacity(self, amount: float = 1.0) -> dict:
        """生成额度预检（不写入）：体验版剩余不足抛 QuotaExceeded，附剩余量（可行动提示）。"""
        data = self._load()
        limit = FREE_GEN_LIMIT
        if self.plan() != "pro" and data["gen_count"] + amount > limit:
            remaining = max(0.0, limit - data["gen_count"])
            raise QuotaExceeded(
                f"本月体验版生成额度不足（剩 {remaining:g} 次，本次需 {amount:g} 次）。"
                "本地执行、自愈与报告不受影响；升级标准版提高生成额度"
                "（档位与获取方式见 mobileaitest.com）。"
                "已购激活码？运行 `mat activate <激活码>` 立即解锁。"
                # 邀请入口就放这里：用户**只在正缺额度的那一刻**才有动机去邀请，
                # 而这条消息本来就是"触顶 → 引导动作"的现成动线。
                # ⚠️ 不写具体奖励数字 —— 那是服务端配置（`INVITE_REWARD`），写死必然漂移（A5）。
                "或者邀请同事用 MAT（运行 `mat invite`），每成功邀请一位可获得额外生成额度。"
            )
        return self.snapshot()

    def consume(self, amount: float = 1.0) -> dict:
        """生成额度核销（写入；调用前应先 ensure_capacity）。"""
        return self._bump("gen_count", amount)

    def ensure_vision_capacity(self, amount: float = 1.0) -> dict:
        """视觉兜底额度预检（不写入）；额度不足抛 QuotaExceeded（两档都有上限）。

        调用方（runner._vision_locate）捕获本异常后**优雅降级**为"vision 未启用"，
        不中断本地执行——vision 是兜底能力，额度耗尽只应退回定位失败。
        """
        data = self._load()
        pro = self.plan() == "pro"
        limit = seat_quota.vision_limit() if pro else FREE_VISION_LIMIT
        if data["vision_count"] + amount > limit:
            remaining = max(0.0, limit - data["vision_count"])
            if pro:
                raise QuotaExceeded(
                    f"本月视觉兜底额度（标准版 {limit} 次/月）已用完，次月自动恢复。"
                    "本次定位退回普通失败（等同未启用视觉兜底）；同屏缓存命中不计费，"
                    "如需更高额度请通过 mobileaitest.com 联系我们。"
                )
            raise QuotaExceeded(
                f"本月体验版视觉兜底额度不足（剩 {remaining:g} 次，本次需 {amount:g} 次）。"
                "本次定位退回普通失败（等同未启用视觉兜底）；升级标准版提高视觉兜底额度"
                "（300 次/月，见 mobileaitest.com）。"
                "或者邀请同事用 MAT（运行 `mat invite`）获得额外额度。"
            )
        return self.snapshot()

    def consume_vision(self, amount: float = 1.0) -> dict:
        """视觉兜底额度核销（写入；调用前应先 ensure_vision_capacity）。"""
        return self._bump("vision_count", amount)

    def check_gen(self, amount: float = 1.0) -> dict:
        """生成前调用：预检 + 计数原子完成（文件锁，消除并发 TOCTOU）。

        交互生成 amount=1；批量导入每条 0.5。
        """
        def _op() -> dict:
            self.ensure_capacity(amount)
            return self.consume(amount)
        return self._locked(_op)

    def check_vision(self, amount: float = 1.0) -> dict:
        """视觉兜底调用前：预检 + 计数原子完成（文件锁，同 check_gen）。

        仅应在**真正调用模型前**触发——缓存命中零 token，不计费（见 vision.locate 的 before_call）。
        """
        def _op() -> dict:
            self.ensure_vision_capacity(amount)
            return self.consume_vision(amount)
        return self._locked(_op)

    # -- 内部 ---------------------------------------------------------------

    def _bump(self, field: str, amount: float) -> dict:
        """读-改-写计数（写盘尽力而为）。

        写失败（HOME 只读/沙箱拦截等异常环境）不抛——额度预检已用最后已知计数
        把过关，这里丢的只是本次增量；把辅助计数升级成整个调用失败，代价是
        用户真实工作被打断（2026-10-13 实测：沙箱化服务进程写不了 ~/.mat ⇒
        视觉兜底整链 PermissionError 失败）。
        """
        data = self._load()
        data[field] = round(data[field] + amount, 2)
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
                # 本地诚实计数下可接受，但降级不该无声（评审 L15）。
                warnings.warn(
                    "当前平台无文件锁（fcntl 不可用），配额计数在并发下可能竞态",
                    RuntimeWarning, stacklevel=2)
            try:
                return fn()
            finally:
                with contextlib.suppress(Exception):
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
