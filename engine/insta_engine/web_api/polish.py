"""AI 润色端点（P1.5，设计 §7.4）：场景描述 → 对齐控件清单真实文案的可生成表述。

同步短调用（不走 job——润色是生成的前置输入，用户要拿着回填结果继续编辑）。
**免费**（不管有没有用润色，AI 生成统一 1 次额度/任务；
润色是提升生成质量的辅助动作，不独立计费、不写 usage）。人数闸按路由表自动
生效（前端必须走 postJson 注入 operator）。
"""

from __future__ import annotations

from pathlib import Path


def api_polish(root: Path, body: dict) -> tuple[int, dict]:
    from ..ai_client import GenClient, GenError
    from ..page_index import controls_context_for

    nl = str(body.get("nl", "")).strip()
    if not nl:
        return 400, {"error": "需要 nl（待润色的场景描述）"}
    package = str(body.get("package", "")).strip()
    app_info: dict = {}
    if package:  # 包名可选：未填时退化为纯文案改写（不注入控件上下文）
        app_info["package"] = package
        controls = controls_context_for(package, root / "artifacts" / "page_index")
        if controls:
            app_info["controls"] = controls
    try:
        text = GenClient.from_env().polish(nl, app_info)
    except GenError as exc:
        return 502, {"error": f"润色失败：{exc}"}
    except Exception as exc:  # noqa: BLE001 —— 网络/配置等未预期错误
        return 502, {"error": f"润色失败：{type(exc).__name__}: {exc}"}
    return 200, {"text": text}
