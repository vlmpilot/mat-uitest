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
送模型的截图可经 `MAT_VL_MAX_EDGE` 缩图（默认关，本地小模型建议 1280）：**只缩
发给模型的那份，坐标换算始终用原图尺寸**——比例契约对缩放免疫，用错尺寸才会跑偏。
"""

from __future__ import annotations

import io
import os
import re
from collections import OrderedDict
from pathlib import Path

CONFIDENCE_THRESHOLD = 0.7
CONFIDENCE_WITHOUT_TREE_EVIDENCE = 0.8

# 送模型前的最长边上限（像素）。**默认 0 = 不缩图**，保持既有云端行为不变：
# 缩小截图是「速度换精度」的权衡，只该由知道自己在做什么的人显式打开。
# 本地小模型（Qwen3-VL-8B-4bit 等）在 1080×2400 上单次 7~8s，设 1280 可降到 3~4s
# （按本地机型实测带宽折算）。
MAX_EDGE_ENV = "MAT_VL_MAX_EDGE"

#: 判定"坐标贴在屏幕边缘"的像素余量。仅用于**标注**（全屏图片、贴边按钮的合法中心
#: 本来就在边缘 1~2px 上，拦掉会误伤），不参与拒绝。
EDGE_MARGIN = 12

_CACHE: OrderedDict[str, tuple[int, int]] = OrderedDict()
_CACHE_MAX = 200  # LRU 上限（整体 clear 会造成缓存抖动）
_LAST_IMAGE_INFO: dict = {}
#: 最近一次判定的原因（诊断用）。`locate()` 只返回坐标或 None，调用方分不清
#: 「模型说没找到」「找到了但置信度不够」「压根没调用」——三者曾被压成同一句
#: 「未命中或未启用」，人只能靠猜（2026-10-26 实测：跑完一条用例，报告里说不清
#: 本地模型到底被调过没有）。
_LAST_RESULT: dict = {}


def last_result() -> dict:
    """最近一次 `locate()`/`parse_vision_response()` 的判定详情（不参与逻辑）。

    键：`ok` / `found` / `confidence` / `threshold` / `reason`。
    """
    return dict(_LAST_RESULT)


def last_image_info() -> dict:
    """最近一次 `locate()` 的图片处理信息（诊断用，不参与逻辑）。

    键：original/sent（宽高元组）、scaled（是否真的缩了）、reason（未缩的原因）。
    """
    return dict(_LAST_IMAGE_INFO)


def _max_edge() -> int:
    raw = (os.environ.get(MAX_EDGE_ENV) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0  # 坏值不猜，退回不缩图（缩图是优化，不能因为它让兜底挂掉）


def prepare_image(screenshot_png: bytes) -> tuple[bytes, dict]:
    """按 `MAT_VL_MAX_EDGE` 决定发给模型的图片；返回 (发送 bytes, 元信息)。

    **只决定"发什么"，不参与坐标换算**——换算必须用**原图**尺寸。这是本函数存在
    的全部理由：此前 `locate()` 把同一份 bytes 既发给模型、又拿去量尺寸，一旦缩图
    就会用缩略图的宽度去乘比例，像素坐标整体偏小。
    比例契约（见模块 docstring）本身对缩放免疫，所以缩图不影响正确性。
    """
    limit = _max_edge()
    if limit <= 0:
        return screenshot_png, {"original": None, "sent": None,
                                "scaled": False, "reason": f"{MAX_EDGE_ENV} 未设"}
    try:
        from PIL import Image
    except ImportError:
        # 走原图：真正缺依赖时 `parse_vision_response` 会给出可行动的报错，
        # 不必在这里重复一次（也避免吞掉那条提示）。
        return screenshot_png, {"original": None, "sent": None,
                                "scaled": False, "reason": "Pillow 缺失"}
    try:
        with Image.open(io.BytesIO(screenshot_png)) as img:
            original = img.size
            long_edge = max(original)
            if long_edge <= limit:
                return screenshot_png, {"original": original, "sent": original,
                                        "scaled": False, "reason": "未超上限"}
            scale = limit / long_edge
            size = (max(1, round(original[0] * scale)), max(1, round(original[1] * scale)))
            resized = img.convert("RGB").resize(size, Image.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG")
        return buffer.getvalue(), {"original": original, "sent": size,
                                   "scaled": True, "reason": f"{MAX_EDGE_ENV}={limit}"}
    except Exception:  # noqa: BLE001 —— 缩图失败退回原图：宁可慢，不可让兜底挂掉
        return screenshot_png, {"original": None, "sent": None,
                                "scaled": False, "reason": "缩图失败"}


def enabled() -> bool:
    if not os.environ.get("GEN_API_KEY"):
        return False
    from . import promptstore
    from .offline import is_loopback

    if is_loopback(endpoint()):
        # 端点在本机：模型名必须由本地配置给（本机服务不会替我们选模型），
        # 所以 `GEN_PROMPT_MODE=server` 的"模型服务端决定"在这里不成立。
        return bool(os.environ.get("GEN_VL_MODEL"))
    if promptstore.is_server():  # 服务端决定 VL 模型，本地无需 GEN_VL_MODEL
        return True
    return bool(os.environ.get("GEN_VL_MODEL"))


def endpoint() -> str:
    """视觉兜底**实际会用**的端点（`GEN_VL_BASE_URL` 优先，缺省回落全局值）。

    抽成函数是为了让「端点在哪」只有一个来源：客户端构造要用它，额度闸也要用它判断
    「这次调用是不是本机模型」。两处各读一遍 env 迟早会漂移——额度闸读错端点，
    就会出现"本地兜底还在扣平台额度"这种自相矛盾的账。

    两者都没配返回空串（调用方按未配置处理），不在这里抛 KeyError：判断"是否本机"
    的调用方不该被迫包异常。
    """
    return os.environ.get("GEN_VL_BASE_URL") or os.environ.get("GEN_BASE_URL", "")


def default_client():
    from . import promptstore
    from .ai_client import GenClient
    from .offline import is_loopback

    # 端点可单独指定（`GEN_VL_BASE_URL`）：`GEN_BASE_URL` 是全局单值，没有这个开关，
    # 「生成走云端、视觉兜底走本地小模型」这种最常见的混合部署就表达不出来——
    # 只能两个都本地（生成质量掉）或两个都云端（截图出本机）。缺省仍回落全局值，
    # 只配 GEN_BASE_URL 的老配置行为不变。
    base_url = endpoint()
    # 🔴 端点在本机 ⇒ **强制走 OpenAI 兼容协议 + 本地 prompt**，与 `GEN_PROMPT_MODE` 无关。
    # `GEN_PROMPT_MODE=server` 的原意是「生成任务的 prompt 由服务端下发」，对视觉兜底
    # 唯一合理的影响是"模型由服务端选"；但 prompt_mode 一旦跟着变 server，请求就会发到
    # `{base_url}/mat/complete`（MAT 私有代理协议）——本机小模型服务（mlx-vlm / ollama /
    # LM Studio）只有 OpenAI 兼容端点，没有这个路径，于是固定 404、兜底一次也调不出去。
    # 2026-10-26 实测：`GEN_PROMPT_MODE=server` + `GEN_VL_BASE_URL=127.0.0.1:8915` 时
    # 客户端 prompt_mode 仍是 server（POST /v1/mat/complete ⇒ 404）。
    local = is_loopback(base_url)
    server = promptstore.is_server() and not local
    return GenClient(
        base_url, os.environ["GEN_API_KEY"],
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

    送模型的是 `prepare_image()` 处理过的图（`MAT_VL_MAX_EDGE` 设了才缩）；坐标换算
    始终拿**原图** `screenshot_png` 量尺寸——这是缩图不跑偏的唯一前提。
    """
    if cache_key and cache_key in _CACHE:
        _CACHE.move_to_end(cache_key)
        _LAST_RESULT.clear()
        _LAST_RESULT.update({"ok": True, "found": True, "confidence": None,
                             "threshold": None, "reason": "同屏缓存命中（未调用模型）"})
        return _CACHE[cache_key]
    if before_call is not None:
        before_call()
    from .ai_client import complete_task_text

    send_png, info = prepare_image(screenshot_png)
    _LAST_IMAGE_INFO.clear()
    _LAST_IMAGE_INFO.update(info)
    content = complete_task_text(
        client, "vision-locate",
        {"intent": intent or "", "target": target or ""},
        "vision-llm 定位", images=[send_png],
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
    """解析 VL 输出并按坐标契约换算像素；红线：低置信/未找到/缺坐标一律 None。

    每个拒绝分支都会在 `last_result()` 里留下可读原因——「拒绝」和「没调用」是
    完全不同的两件事，报告不该让用户猜。
    """
    threshold = CONFIDENCE_THRESHOLD if tree_evidence else CONFIDENCE_WITHOUT_TREE_EVIDENCE
    _LAST_RESULT.clear()
    if not isinstance(data, dict) or not data.get("found"):
        _LAST_RESULT.update({"ok": False, "found": False, "confidence": 0.0,
                             "threshold": threshold, "reason": "模型判未命中（found=false）"})
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        _LAST_RESULT.update({"ok": False, "found": True, "confidence": 0.0,
                             "threshold": threshold, "reason": "置信度字段非法"})
        return None
    if confidence < threshold:
        _LAST_RESULT.update({
            "ok": False, "found": True, "confidence": confidence, "threshold": threshold,
            "reason": f"置信度 {confidence:g} 低于门禁 {threshold:g}（已拒绝，不盲点）",
        })
        return None
    x, y = data.get("x"), data.get("y")
    if x is None or y is None:
        _LAST_RESULT.update({"ok": False, "found": True, "confidence": confidence,
                             "threshold": threshold, "reason": "模型没给坐标"})
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
        pos = (round(x * screen_w), round(y * screen_h))
    elif 0 < x <= 1000 and 0 < y <= 1000 and (screen_w > 1000 or screen_h > 1000):
        # ② 千分比兜底（仅大屏参与判定）
        pos = (round(x / 1000.0 * screen_w), round(y / 1000.0 * screen_h))
    else:  # ③ 像素直接使用
        pos = (round(x), round(y))

    # 屏幕外坐标必然无效：模型给出 (2000,3000) 时旧代码照样返回，定位器会照着点出去。
    # 生产数据里已有贴边样本（`(1,1271)` 落在屏幕最左 1px），说明坐标换算歧义是真实
    # 存在而非理论风险。**只拦严格越界**——贴边不拦，见 EDGE_MARGIN 的说明。
    if not (0 <= pos[0] < screen_w and 0 <= pos[1] < screen_h):
        _LAST_RESULT.update({
            "ok": False, "found": True, "confidence": confidence, "threshold": threshold,
            "reason": f"坐标 {pos} 超出屏幕 {screen_w}×{screen_h}（已拒绝，不点屏幕外）",
            "edge": False,
        })
        return None
    edge = min(pos[0], pos[1], screen_w - 1 - pos[0], screen_h - 1 - pos[1]) <= EDGE_MARGIN
    _LAST_RESULT.update({
        "ok": True, "found": True, "confidence": confidence, "threshold": threshold,
        "reason": "命中（贴屏幕边缘，注意坐标换算歧义）" if edge else "命中",
        "edge": edge,
    })
    return pos
