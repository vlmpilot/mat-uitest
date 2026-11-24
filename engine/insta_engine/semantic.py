"""语义断言（评审 #14）：用视觉模型判断一句自然语言是否成立。

## 为什么必须显式开启

控件断言是**确定性**的；这里是**概率性**的。默认开启等于把"模型说成立"当成"真的成立"，
与项目红线（禁止盲点）冲突。所以：

- `MAT_SEMANTIC_ASSERT=1` 才启用。未启用时断言**失败并说明原因**——不是静默跳过：
  "我以为它在验，其实没验"是最坏的一种绿灯。
- 结论按「截图指纹 + 断言语句」缓存（与视觉兜底共用 `MAT_VL_CACHE` 配置）。
- 模型给的理由写进 `_LAST_RESULT`，报告与 `run.json` 能看到它凭什么这么说。

## 判定口径

模型返回 `{ok, confidence, reason}`。**ok=True 且 confidence ≥ 阈值**才算成立；
低置信一律不成立——"不确定"在断言语义里等于"没证明"。阈值故意与视觉定位一致（0.7），
免得同一个模型在不同环节被两套标准衡量。
"""

from __future__ import annotations

import hashlib
import io
import os
import re

ENV_FLAG = "MAT_SEMANTIC_ASSERT"
#: 与视觉定位同一阈值（`vision.CONFIDENCE_THRESHOLD`）：同一模型不该有两套标准。
THRESHOLD = 0.7

_LAST: dict = {}

#: 进程内缓存（key = 截图哈希 + 断言语句）。落盘复用 `vision` 的那套不是巧合——
#: 同一个屏反复断言同一句话在 CI 里很常见，两边各存一份只会让"要不要开落盘"变成两道配置。
_CACHE: dict[str, tuple[bool, str]] = {}
_CACHE_MAX = 500


def enabled() -> bool:
    """是否启用语义断言（显式开启才生效）。"""
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def last_result() -> dict:
    return dict(_LAST)


def _parse(text: str) -> tuple[bool, float, str]:
    """从模型输出里提 ok / confidence / reason（模型常带代码围栏或解释）。"""
    raw = str(text or "")
    ok_match = re.search(r'"ok"\s*:\s*(true|false)', raw, re.IGNORECASE)
    ok = bool(ok_match and ok_match.group(1).lower() == "true")
    conf_match = re.search(r'"confidence"\s*:\s*([\d.]+)', raw)
    try:
        confidence = float(conf_match.group(1)) if conf_match else 0.0
    except ValueError:
        confidence = 0.0
    reason_match = re.search(r'"reason"\s*:\s*"([^"]*)"', raw)
    reason = (reason_match.group(1) if reason_match else "").strip()
    return ok, confidence, reason


def judge(drv, claim: str) -> tuple[bool, str]:
    """判断 `claim` 是否成立；返回 `(是否成立, 理由)`。

    任何异常都转成"不成立 + 原因"：断言环节不该抛驱动/网络异常让调用方去猜分类。
    """
    from . import vision

    _LAST.clear()
    try:
        image = drv.screenshot_pil()
        if image is None:
            _LAST.update({"ok": False, "reason": "截图不可用"})
            return False, "截图不可用"
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        png = buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        _LAST.update({"ok": False, "reason": f"截图失败: {type(exc).__name__}"})
        return False, f"截图失败: {type(exc).__name__}: {exc}"

    key = hashlib.md5(png).hexdigest() + ":" + claim  # noqa: S324 —— 缓存键，非安全用途
    if key in _CACHE:
        ok, reason = _CACHE[key]
        _LAST.update({"ok": ok, "reason": reason, "cached": True})
        return ok, reason

    if not vision.enabled():
        _LAST.update({"ok": False, "reason": "视觉端点未配置，语义断言无法判定"})
        return False, "视觉端点未配置（GEN_VL_MODEL / GEN_API_KEY）"

    try:
        from .ai_client import complete_task_text

        client = vision.default_client()
        content = complete_task_text(client, "semantic-assert", {"claim": claim},
                                     "语义断言", images=[png])
        ok, confidence, reason = _parse(content)
    except Exception as exc:  # noqa: BLE001 —— 调用失败按不成立处理，原因带上
        _LAST.update({"ok": False, "reason": f"调用异常: {type(exc).__name__}: {exc}"})
        return False, f"调用异常: {type(exc).__name__}: {exc}"

    if ok and confidence < THRESHOLD:
        reason = (f"模型判成立但置信度 {confidence:g} < {THRESHOLD:g}，按不成立处理"
                  f"（{reason or '无理由'}）")
        ok = False
    if not reason:
        reason = "模型未给出理由"
    _LAST.update({"ok": ok, "confidence": confidence, "reason": reason, "cached": False,
                  "threshold": THRESHOLD})
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()  # 断言缓存总量有限，整体清可接受（不像坐标缓存那样高频）
    _CACHE[key] = (ok, reason)
    return ok, reason


def reset() -> None:
    """清缓存（测试用）。"""
    _CACHE.clear()
    _LAST.clear()
