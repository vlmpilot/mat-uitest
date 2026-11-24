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
import json
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
_CACHE_MAX = 200  # 内存 LRU 上限（整体 clear 会造成缓存抖动）

#: 缓存模式（评审 #12）：`memory`=仅进程内（默认，行为不变）；
#: `disk`=额外落盘到 `state_home/vision_cache.json`（CI 复用，同一屏同一目标不重复付费）；
#: `off`=不用缓存（调试"真的有没有调模型"时用）。
CACHE_MODE_ENV = "MAT_VL_CACHE"
_DISK_FILE = "vision_cache.json"
_DISK_MAX = 5000  # 落盘上限：再多就是噪声（且 json 全量读写会变慢）
_disk: dict | None = None          # 懒加载的落盘内容
_disk_override: Path | None = None  # 测试注入路径


def _cache_mode() -> str:
    mode = (os.environ.get(CACHE_MODE_ENV) or "memory").strip().lower()
    return mode if mode in ("memory", "disk", "off") else "memory"


def _disk_path() -> Path:
    if _disk_override is not None:
        return _disk_override
    from .state import state_home

    return state_home() / _DISK_FILE


def _load_disk() -> dict:
    global _disk
    if _disk is None:
        try:
            data = json.loads(_disk_path().read_text(encoding="utf-8"))
            _disk = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            _disk = {}
    return _disk


def _save_disk() -> None:
    """落盘（失败不抛：缓存是省钱设施，写不进去不该让兜底失败）。"""
    data = _load_disk()
    if len(data) > _DISK_MAX:
        # 按时间淘汰：坐标缓存的价值随时间下降（App 改版后旧条目再也不会命中）
        oldest = sorted(data.items(), key=lambda kv: str(kv[1].get("ts", "")))[:-_DISK_MAX]
        for key, _val in oldest:
            data.pop(key, None)
    try:
        path = _disk_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _current_model() -> str:
    return os.environ.get("GEN_VL_MODEL", "")


def _cache_get(key: str) -> tuple[int, int] | None:
    """缓存查询：内存 → （disk 模式）落盘。"""
    mode = _cache_mode()
    if mode == "off":
        return None
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    if mode == "disk":
        entry = _load_disk().get(key)
        # **换模型不混用**：不同模型的偏差不同，坐标不能跨模型复用。
        if isinstance(entry, dict) and entry.get("model") == _current_model():
            point = entry.get("point")
            if isinstance(point, list) and len(point) == 2:
                hit = (int(point[0]), int(point[1]))
                _CACHE[key] = hit
                _CACHE.move_to_end(key)
                return hit
    return None


def _cache_put(key: str, point: tuple[int, int]) -> None:
    mode = _cache_mode()
    if mode == "off":
        return
    _CACHE[key] = point
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    if mode == "disk":
        from datetime import UTC, datetime

        _load_disk()[key] = {
            "point": [int(point[0]), int(point[1])],
            "model": _current_model(),
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _save_disk()


def reset_cache(mode: str | None = None) -> None:
    """清空内存缓存（测试/切模型用）；`mode` 给定时顺带改缓存模式。"""
    global _disk
    _CACHE.clear()
    if mode is not None:
        os.environ[CACHE_MODE_ENV] = mode
    if mode == "off":
        _disk = None
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


#: 按模型名识别 GUI 专用模型族。**只映射输出格式已确认的**：GUI-Owl 之类暂不映射——
#: 没见过它的真实输出，凭猜写解析器只会造出"看起来支持"的假象。
_MODEL_FAMILIES: dict[str, tuple[str, ...]] = {
    "uitars": ("ui-tars", "uitars", "ui_tars"),
}


def model_family(model: str) -> str:
    """模型族（决定用哪份 prompt 与解析路径）；认不出返回 "generic"。"""
    name = (model or "").lower()
    for family, keys in _MODEL_FAMILIES.items():
        if any(k in name for k in keys):
            return family
    return "generic"


def prompt_name(model: str | None = None) -> str:
    """该模型该用的 prompt 名（`vision-locate-<family>`，无专用模板时退回通用）。"""
    from . import promptstore

    family = model_family(model if model is not None else os.environ.get("GEN_VL_MODEL", ""))
    if family != "generic" and promptstore.resolve(f"vision-locate-{family}") is not None:
        return f"vision-locate-{family}"
    return "vision-locate"


def enabled() -> bool:
    from . import promptstore
    from .offline import is_private_endpoint

    private = is_private_endpoint(endpoint())
    # 私有端点（本机 / MAT_PRIVATE_HOSTS）常是内网网关，可能不校验 Key：不强制要求。
    # 公网端点（官方代理由服务端校验、BYOK 由厂商校验）仍要 Key。
    if not private and not os.environ.get("GEN_API_KEY"):
        return False
    if private:
        # 私有端点的模型名必须由本地配置给（本机/内网服务不会替我们选模型），
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
    from .offline import is_private_endpoint

    # 端点可单独指定（`GEN_VL_BASE_URL`）：`GEN_BASE_URL` 是全局单值，没有这个开关，
    # 「生成走云端、视觉兜底走本地小模型」这种最常见的混合部署就表达不出来——
    # 只能两个都本地（生成质量掉）或两个都云端（截图出本机）。缺省仍回落全局值，
    # 只配 GEN_BASE_URL 的老配置行为不变。
    base_url = endpoint()
    # 🔴 端点是私有端点（本机 / MAT_PRIVATE_HOSTS）⇒ **强制走 OpenAI 兼容协议 + 本地 prompt**，
    # 与 `GEN_PROMPT_MODE` 无关。内网 GPU 机与本机同语义：它们都只提供 OpenAI 兼容端点，
    # 走官方代理的私有协议会固定 404（2026-10-26 本机、2026-11-23 内网同一类 bug）。
    # `GEN_PROMPT_MODE=server` 的原意是「生成任务的 prompt 由服务端下发」，对视觉兜底
    # 唯一合理的影响是"模型由服务端选"；但 prompt_mode 一旦跟着变 server，请求就会发到
    # `{base_url}/mat/complete`（MAT 私有代理协议）——本机小模型服务（mlx-vlm / ollama /
    # LM Studio）只有 OpenAI 兼容端点，没有这个路径，于是固定 404、兜底一次也调不出去。
    # 2026-10-26 实测：`GEN_PROMPT_MODE=server` + `GEN_VL_BASE_URL=127.0.0.1:8915` 时
    # 客户端 prompt_mode 仍是 server（POST /v1/mat/complete ⇒ 404）。
    local = is_private_endpoint(base_url)
    server = promptstore.is_server() and not local
    # 按模型族选模板（评审 #12）：GUI 专用模型有自己训练时的输出格式，通用 JSON prompt
    # 发挥不出它的水平。专用模板不存在时退回通用——**不做静默降级之外的假装支持**。
    prompt_path = None
    if not server:
        prompt_path = (promptstore.resolve(prompt_name())
                       or Path("prompts/vision-locate.prompt"))
    return GenClient(
        base_url, os.environ.get("GEN_API_KEY", ""),
        os.environ.get("GEN_VL_MODEL", ""),
        prompt_path,
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
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            _LAST_RESULT.clear()
            _LAST_RESULT.update({"ok": True, "found": True, "confidence": None,
                                 "threshold": None,
                                 "reason": "同屏缓存命中（未调用模型）"})
            return cached
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
        extract_fields(content), screenshot_png, tree_evidence=tree_evidence, target=target
    )
    if parsed and cache_key:
        _cache_put(cache_key, parsed)
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

    # desc 是**模型有没有真的读出控件**的唯一证据（坐标不可验证，文案可以）。
    desc = re.search(r'"desc"\s*:\s*"([^"]*)"', text) or \
        re.search(r"'desc'\s*:\s*'([^']*)'", text)
    fields["desc"] = (desc.group(1) if desc else "").strip()

    x_match = re.search(r'"?x"?\s*:\s*\[?\s*([\d.]+)(?:\s*,\s*([\d.]+))?', text)
    fields["confidence_given"] = confidence is not None
    y_match = re.search(r'"?y"?\s*:\s*\[?\s*([\d.]+)', text)
    fields["x"] = float(x_match.group(1)) if x_match else None
    if y_match:
        fields["y"] = float(y_match.group(1))
    elif x_match and x_match.group(2):
        # 模型把 x/y 挤在一起输出（如 "x": 438, 124）——第二个数按 y 处理
        fields["y"] = float(x_match.group(2))
    else:
        fields["y"] = None

    # GUI 专用模型（UI-TARS）的 box 输出：<|box_start|>(x1,y1),(x2,y2)<|box_end|>，
    # 坐标是 0~1000 千分比。取 box 中心换算成**比例**（走 ① 分支，不受屏幕尺寸限制）。
    if fields["x"] is None:
        box = re.search(
            r"<\|box_start\|>\s*\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)?\s*,"
            r"\s*\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)?\s*<\|box_end\|>",
            text,
        )
        if box:
            x1, y1, x2, y2 = (float(g) for g in box.groups())
            fields["box_permille"] = [x1, y1, x2, y2]
            if x1 == x2 == y1 == y2 == 0:
                # prompt 约定的"没找到"写法（全零 box）
                fields["found"] = False
            else:
                fields["found"] = True
                fields["x"] = ((x1 + x2) / 2) / 1000.0
                fields["y"] = ((y1 + y2) / 2) / 1000.0
    return fields


#: 模型没真读控件时会吐回的占位词（prompt 里的示例值）。
#: 2026-11-02 实测：目标在页面上不存在时，8B 仍会 found=true / confidence=0.95 /
#: 坐标 (0.137,0.383) / desc 原样回显 "元素简述"——**置信度和坐标都不可信，文案才是破绽**。
_PLACEHOLDER_DESCS = {
    "元素简述", "元素描述", "简述", "描述", "说明", "示例", "无",
    "desc", "description", "element", "text", "...", "…",
}


def desc_verdict(desc: str, target: str = "") -> tuple[bool, str]:
    """模型给的 `desc` 可不可信：返回 `(ok, 原因)`。

    **只观测、不拦截**（2026-11-02）：真实调用量还是 0，没法定阈值。先攒数据。
    `ok=False` 里"占位词/空"是**硬证据**（它根本没看控件），"与目标无交集"只是弱信号
    （模型完全可以说"齿轮图标"来指"设置"）。
    """
    d = (desc or "").strip()
    if not d:
        return False, "desc 为空"
    if d.strip("。.… ") in _PLACEHOLDER_DESCS:
        return False, "desc 是占位词"
    t = (target or "").strip()
    if t and t not in d and d not in t:
        return False, "desc 与目标无交集"
    return True, ""


def parse_vision_response(data: dict, screenshot_png: bytes,
                          tree_evidence: bool = True,
                          target: str = "") -> tuple[int, int] | None:
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
        _hint = ""
        if data.get("box_permille") and not data.get("confidence_given"):
            _hint = ("（该模型只给了 box、没给 confidence：请在 prompt 里明确要求该字段；"
                     "按红线拒绝——不盲点）")
        _LAST_RESULT.update({
            "ok": False, "found": True, "confidence": confidence, "threshold": threshold,
            "reason": f"置信度 {confidence:g} 低于门禁 {threshold:g}（已拒绝，不盲点）{_hint}",
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
    # 文案审计（只观测不拦截）：模型说 found=true 时，它有没有真的读出控件？
    # 坐标无法事后验证（除非有树），文案可以——占位词就是"它没看"的硬证据。
    desc = str(data.get("desc") or "")
    desc_ok, desc_reason = desc_verdict(desc, target)
    _LAST_RESULT.update({
        "ok": True, "found": True, "confidence": confidence, "threshold": threshold,
        "reason": "命中（贴屏幕边缘，注意坐标换算歧义）" if edge else "命中",
        "edge": edge,
        "desc_ok": desc_ok, "desc_reason": desc_reason, "desc_len": len(desc.strip()),
    })
    from . import ai_metrics

    ai_metrics.record_vision_audit(
        desc_ok=desc_ok, reason=desc_reason, confidence=confidence,
        desc_len=len(desc.strip()), target_len=len((target or "").strip()),
    )
    return pos
