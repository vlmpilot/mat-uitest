"""vision-llm 定位兜底（策略链最后一级）。

触发条件：文本/资源定位链与本地自愈全部失败后；GEN_VL_MODEL 配置时启用。
坐标契约（吸收同行经验 2026-09-27）：**比例（0~1）是唯一首选契约**——像素与 0~1000
千分比在小屏上区间重叠易误判（真机实测 y 偏差可达 1380px），比例无歧义；且比例在
缩放图与原图上等价，天然免疫分辨率缩放。消费侧三档识别：ratio（严格 <1.0）→
permille（≤1000 且屏幕边 >1000）→ pixel。
红线（02 §3.2）：置信度不达标或 found=false 时拒绝返回坐标——禁止盲点。
置信度门禁：默认 0.7；目标文本不在 UI 树中（无在场证据）时提升至 0.8——
只采纳"清晰看到目标本身"级别的答案（同行线上案例：0.7 误点近似文案进错页面）。
结果按「屏幕哈希+意图+目标」缓存（同屏重试不重复计费）。
"""

from __future__ import annotations

import io
import os
import re
from collections import OrderedDict
from pathlib import Path

CONFIDENCE_THRESHOLD = 0.7
CONFIDENCE_WITHOUT_TREE_EVIDENCE = 0.8

_CACHE: OrderedDict[str, tuple[int, int]] = OrderedDict()
_CACHE_MAX = 200  # LRU 上限（整体 clear 会造成缓存抖动）


def enabled() -> bool:
    if not os.environ.get("GEN_API_KEY"):
        return False
    from . import promptstore

    if promptstore.is_server():  # 服务端决定 VL 模型，本地无需 GEN_VL_MODEL
        return True
    return bool(os.environ.get("GEN_VL_MODEL"))


def default_client():
    from . import promptstore
    from .ai_client import GenClient

    server = promptstore.is_server()
    return GenClient(
        os.environ["GEN_BASE_URL"], os.environ["GEN_API_KEY"],
        os.environ.get("GEN_VL_MODEL", ""),
        None if server else Path("prompts/vision-locate.prompt"),
        prompt_mode="server" if server else "local",
    )


def locate(client, screenshot_png: bytes, intent: str, target: str,
           cache_key: str | None = None, tree_evidence: bool = True,
           before_call=None) -> tuple[int, int] | None:
    """返回命中元素的像素中心坐标 (x, y)；未命中/低置信返回 None。

    门控（enabled()）由调用方（runner）负责；本函数保持纯净便于注入测试。
    tree_evidence=False 表示目标文本不在 UI 树中，置信度门禁提升至 0.8。
    before_call：**真正调用模型前**执行的回调（runner 用它核销免费档额度）。
    缓存命中不触发回调——同屏重试零 token 也就零计费；回调抛出的异常原样上抛，
    由调用方决定降级策略（runner 捕获后等同"vision 未启用"）。

    prompt 经统一任务入口（`ai_client.complete_task_text`）：server 模式下由轻云组装，
    本地不再需要 prompts/vision-locate.prompt。
    """
    if cache_key and cache_key in _CACHE:
        _CACHE.move_to_end(cache_key)
        return _CACHE[cache_key]
    if before_call is not None:
        before_call()
    from .ai_client import complete_task_text

    content = complete_task_text(
        client, "vision-locate",
        {"intent": intent or "", "target": target or ""},
        "vision-llm 定位", images=[screenshot_png],
    )
    parsed = parse_vision_response(
        extract_fields(content), screenshot_png, tree_evidence=tree_evidence
    )
    if parsed and cache_key:
        _CACHE[cache_key] = parsed
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return parsed


def extract_fields(text: str) -> dict:
    """容错提取 VL 输出字段（模型常输出非法 JSON，如 "x": 438, 124 或单引号）。

    规则优先于模型（02 §3.5 同哲学）：正则提取 > 严格 JSON 解析。
    """
    text = str(text)
    fields: dict = {}

    found = re.search(r'"?found"?\s*:\s*(true|false)', text, re.IGNORECASE)
    fields["found"] = bool(found and found.group(1).lower() == "true")

    confidence = re.search(r'"?confidence"?\s*:\s*([\d.]+)', text)
    try:
        fields["confidence"] = float(confidence.group(1)) if confidence else 0.0
    except ValueError:
        fields["confidence"] = 0.0

    x_match = re.search(r'"?x"?\s*:\s*\[?\s*([\d.]+)(?:\s*,\s*([\d.]+))?', text)
    y_match = re.search(r'"?y"?\s*:\s*\[?\s*([\d.]+)', text)
    fields["x"] = float(x_match.group(1)) if x_match else None
    if y_match:
        fields["y"] = float(y_match.group(1))
    elif x_match and x_match.group(2):
        # 模型把 x/y 挤在一起输出（如 "x": 438, 124）——第二个数按 y 处理
        fields["y"] = float(x_match.group(2))
    else:
        fields["y"] = None
    return fields


def parse_vision_response(data: dict, screenshot_png: bytes,
                          tree_evidence: bool = True) -> tuple[int, int] | None:
    """解析 VL 输出并按坐标契约换算像素；红线：低置信/未找到/缺坐标一律 None。"""
    if not isinstance(data, dict) or not data.get("found"):
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    threshold = CONFIDENCE_THRESHOLD if tree_evidence else CONFIDENCE_WITHOUT_TREE_EVIDENCE
    if confidence < threshold:
        return None
    x, y = data.get("x"), data.get("y")
    if x is None or y is None:
        return None

    try:
        from PIL import Image
    except ImportError as exc:
        # Pillow 已是主依赖；此处兜底给**可行动**的报错，而不是裸 ModuleNotFoundError
        # （评审 L10：视觉兜底不应因上游依赖变化而静默失效）。
        raise RuntimeError(
            "视觉坐标换算需要 Pillow：pip install pillow（或 pip install 'mat-uitest[image]'）"
        ) from exc
    screen_w, screen_h = Image.open(io.BytesIO(screenshot_png)).size

    # 三档坐标契约识别（像素与千分比在小屏重叠，比例无歧义）
    if 0 < x < 1.0 and 0 < y < 1.0:  # ① 比例（严格 <1.0，避免把像素 (1,1) 误判为比例）
        return (round(x * screen_w), round(y * screen_h))
    if 0 < x <= 1000 and 0 < y <= 1000 and (screen_w > 1000 or screen_h > 1000):
        # ② 千分比兜底（仅大屏参与判定）
        return (round(x / 1000.0 * screen_w), round(y / 1000.0 * screen_h))
    # ③ 像素直接使用
    return (round(x), round(y))
