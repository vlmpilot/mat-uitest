"""NL → DSL 生成客户端（open-core 边界：prompt 属闭源资产，见 docs/13-产品使用手册.md）。

两种模式（`GEN_PROMPT_MODE`）：
- `local`（默认，自带 Key 直连）：prompt 从工作区 `prompts/*.prompt` 读取并本地拼装；
- `server`（轻云代理，方案 A）：**prompt 由服务端组装**，客户端只发原始材料（nl / IR / 控件清单 /
  截图 / 意图），MaaS Key 与闭源 prompt 都不落到本机。

统一入口是 `complete_task_text` / `complete_task_raw`——vision 与手工导入也走同一条路，
保证「切到 server 模式后没有任何一条链路仍在本地拼 prompt」。
generate_calibrated 实现 Dry-run 校准：真机 dry-run 校准回路（≤3 轮失败回喂）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from .dsl import TestCase, dump_dsl

if TYPE_CHECKING:  # 能力层不依赖编排层
    from .runner import RunResult

# 与 prompts/ 目录一一对应
TASKS = ("gen", "convert", "map", "vision-locate", "describe")


def apply_quality_gate(case: TestCase, index_dir: Path | None = None) -> TestCase:
    """把**用例质量闸门**套在生成产物上（与 `mat import` 共用同一实现）。

    为什么必须在这里做：`mat gen` 的产物此前**一道闸都没有**（schema 校验通过就落盘），
    而它与导入产物**跑在同一台设备上** —— 同一件事两个口径必然漂移。
    实测（对 52 条历史跑测的失败逐条归因）：37 条失败里 **27 条带确定性用例缺陷**。

    闸门做两件事（见 `case_quality` 的模块说明）：
      · **消毒**（自动改）：状态后缀剥离、断言缺定位时继承本步目标、继承不了就删；
      · **检查**（只报）：不可见字符定位 / 未切 App / 起始状态无人保证 / 导航耦合断言 …
    issues 写进 `meta.quality_issues` —— **持久化**到用例文件里，
    这样人**跑之前**（校对时）和**跑之后**（归因时）都能看到，而不是只在命令行刷一下。

    `index_dir`（可选）：页面索引目录。给了它才跑 `case_audit` 的**清单校验（R18/R19）**——
    那两条要拿控件清单/弹窗词表，`case_quality` 判不了（见其模块说明的分工表）。
    ⚠️ **为什么必须在生成路径补这一环**：此前 R18/R19 只在 `mat import` 与配置台保存时跑，
    **`mat gen` 不跑** ⇒ "AI 只能操作真实存在的控件"这道闸在生成路径上**只有 prompt 层注入，
    没有事后校验**（评审核实出的唯一不准确项）。这里补上，且**只有一处实现**。
    """
    from .case_quality import review

    raw = json.loads(dump_dsl(case))

    def _rewrite(document: dict) -> list[str]:
        """要图谱的两处**确定性改写**（跑在 `review` 的检查之前，见其顺序说明）。

        ① 共用 res-id → 文案（R21）：7 个频道 tab 共用 `channelbar_text` 这类，
           靠本步 intent 定到唯一一条才改；
        ② 导航耦合断言 → 状态断言（R25）：点完控件**还在**落点页 ⇒ 断言恒真，
           改成 `selected` 才有区分度。
        顺序不能反（② 依赖 ① 把定位换成**唯一文案**）—— 所以走 `case_audit.index_rewrites`
        这一个入口，不在调用方各挑一个。
        """
        from .case_audit import index_rewrites

        return index_rewrites(document, index_dir)

    issues = review(raw, rewrite=_rewrite if index_dir is not None else None)
    # ⚠️ **无论有没有 issue，都要用消毒后的 raw 重建用例** ——
    # 第一版写成 `if issues: case = validate(raw)`，于是"只消毒没报 issue"的情况
    # （例如仅剥掉 `视频，可选中` 的状态后缀）**改动被静默丢弃**。
    # 测试 `test_generate_quality_gate_survives_dump_roundtrip` 抓到了它。
    case = TestCase.model_validate(raw)
    if index_dir is not None:
        # 审计要跑在**消毒后**的用例上（否则会报已被人为剥掉的状态后缀）；延迟导入避免循环。
        from .case_audit import audit_case

        issues += audit_case(case, index_dir)
    if issues:
        case.meta["quality_issues"] = issues
    return case


def _validate_single(data) -> TestCase:
    """单用例校验；兼容模型跟上拆分 prompt 输出 {"cases":[...]} —— 取第一条。

    generate()/generate_calibrated() 是「单用例」语义的调用方（校准回路逐条跑、
    CLI 落单文件），共用 gen prompt 时模型可能返回数组——取首条而非硬报错。
    """
    if isinstance(data, dict) and isinstance(data.get("cases"), list) and data["cases"]:
        data = data["cases"][0]
    return TestCase.model_validate(data)


def _validate_multi(data) -> tuple[list[TestCase], list[str]]:
    """多用例校验（拆分生成）：{"cases":[...]} / [...] / 单对象 {...} 三形态兼容。

    salvage：单条 schema 校验失败不整批作废——合法条目保留（与 describe_pages T53c
    同口径），全部非法才抛错触发回喂重试（T56 机制不变）。
    """
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        cases = data["cases"]
    elif isinstance(data, list):
        cases = data
    elif isinstance(data, dict):  # 模型没跟上数组格式——包一层
        cases = [data]
    else:
        raise GenError("生成输出缺少 cases 数组")
    if not cases:
        raise GenError("cases 数组为空")
    items: list[TestCase] = []
    warns: list[str] = []
    for i, raw in enumerate(cases, 1):
        try:
            items.append(TestCase.model_validate(raw))
        except Exception as exc:  # noqa: BLE001 —— 单条坏不整批死（schema 错误形态多样）
            warns.append(f"cases 第 {i} 条校验失败已丢弃: {str(exc)[:200]}")
    if not items:
        raise GenError(f"cases 中没有任何合法用例（{'；'.join(warns)[:400]}）")
    return items, warns


_last_request_at = [0.0]  # 进程级请求时间戳（MAT_REQUEST_MIN_INTERVAL 节流用，跨客户端共享）


# 环境变量数值读取容错（2026-10-12 评审）：`_content_repairs` 处曾因裸 int() 读到
# 非数字导致**整个 engine 无法 import**（注释见 :168 起），但同文件其余六处
# int()/float() env 读仍是裸的——一个坏值就能让 describe/run 全挂。统一走这里。
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_opt_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _env_opt_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class GenError(RuntimeError):
    def __init__(self, msg: str, content: str | None = None):
        super().__init__(msg)
        self.content = content  # 原始模型输出（截断恢复用，T53c）


# -- 任务级统一入口（local / server 双模式） -------------------------------------


def task_mode(client) -> str:
    """取客户端的 prompt 模式；鸭子类型对象（测试桩）默认按 local 处理。"""
    return getattr(client, "prompt_mode", "local")


def substitute(prompt: str, variables: dict) -> str:
    """占位符替换：字符串按原文注入，其余按 JSON 注入（与代理侧口径一致）。"""
    text = prompt
    for key, value in (variables or {}).items():
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = text.replace("{" + key + "}", rendered)
    return text


def gen_variables(nl: str, app_info: dict | None) -> dict:
    """gen 的模板变量：nl / app_info / 可选的 few-shot 示例。

    示例按 `app_info.package` 检索同 App 的**已通过**用例（见 `fewshot.py`）；
    没有 package 或开关未开时为空，行为与之前完全一致。

    做成模块级函数而不是 `GenClient` 方法：测试桩是鸭子类型对象，没有实例方法。
    """
    variables: dict = {"nl": nl, "app_info": app_info or {}}
    from . import fewshot

    examples = fewshot.build(str((app_info or {}).get("package") or ""), nl)
    if examples:
        variables["examples"] = examples
    return variables


def _coverage_rounds(case, missing: list[str]) -> list[dict]:
    """把缺失动作回喂：带上已有用例，要求只补不删。

    整条重生成的代价是把已经对的步骤也可能改坏；带上原产物做增量修正更稳。
    """
    return [
        {"role": "assistant", "content": case.model_dump_json(by_alias=True)},
        {"role": "user", "content": (
            "上面的用例缺少需求里的这些动作：\n- " + "\n- ".join(missing) +
            "\n请在**保持已有步骤不变**的前提下补齐，输出完整的用例 JSON（同一 schema）。")},
    ]


def _finalize_gen(client, nl: str, app_info: dict | None, index_dir, validator,
                  case, allow_retry: bool = True):
    """质量闸 →（可选）缺步骤补一次 → 覆盖标记。"""
    from . import step_coverage

    gated = apply_quality_gate(case, index_dir)
    if allow_retry and step_coverage.retry_enabled():
        missing = step_coverage.missing(nl, gated)
        if missing:
            try:
                retried = complete_task_raw(
                    client, "gen", gen_variables(nl, app_info), "生成(补步骤)",
                    validator=validator, rounds=_coverage_rounds(gated, missing))
                gated = apply_quality_gate(retried, index_dir)
            except GenError:
                # 补步骤失败不该让整次生成失败——保留原产物，由 R28 如实报出来
                pass
    _attach_coverage(gated, nl)
    # nl 回溯（2026-10-28，微调工具链 L2 的数据地基）：生成时的原始需求随用例落盘，
    # 此前的 2,300+ 条 run.json 全部没有 nl ⇒ 历史数据无法构成 (nl → DSL) 训练对。
    # 只存不用于执行链路；`mat finetune export` 消费。
    if nl and not gated.meta.get("nl"):
        gated.meta["nl"] = nl[:2000]
    return gated


def _attach_coverage(case, nl: str) -> None:
    """把「需求有动作、步骤没承载」写进 `meta.quality_issues`（只报不改）。"""
    from . import step_coverage

    issues = step_coverage.missing(nl, case)
    if not issues:
        return
    bucket = case.meta.setdefault("quality_issues", [])
    bucket.extend(issues)


def _assemble_local(task: str, variables: dict) -> str:
    from . import promptstore

    prompt = promptstore.read(task)
    if prompt is None:
        raise GenError(
            f"未找到 {task} prompt；请先运行 `mat init` 播种 prompts/（包内自带 *.prompt.example 模板）"
        )
    # 用户追加段（prompts/<task>.append.md）拼在模板尾部——定制不复制整模板，
    # 官方模板更新照常生效（「模板冻结」的另一半解法是 promptstore.staleness）
    appended = promptstore.read_append(task)
    if appended:
        prompt = f"{prompt}\n\n{appended}"
    text = substitute(prompt, variables)
    # few-shot 示例：模板有 `{examples}` 时已随 substitute 注入；没有就追加到末尾。
    # 追加而不是改模板，是为了不打断已有三处 prompt 的同步纪律（改模板要同时改
    # prompts/*.example 与 engine/insta_engine/prompts/*.example，漂移有测试守着）。
    examples = variables.get("examples")
    if examples and "{examples}" not in prompt:
        text = f"{text}\n\n{examples}"
    return text


def _messages_from(text: str, images: list[bytes] | None = None,
                   rounds: list[dict] | None = None) -> list[dict]:
    """组装 messages：图片在前、文本在后（vision 口径）；rounds 为校准回喂的追加轮次。"""
    if images:
        content: list[dict] | str = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{base64.b64encode(img).decode('ascii')}"}}
            for img in images
        ]
        content.append({"type": "text", "text": text})
    else:
        content = text
    messages: list[dict] = [{"role": "user", "content": content}]
    if rounds:
        messages.extend(rounds)
    return messages


_JSON_MODE_STATE: dict[str, bool] = {"unsupported": False}


def _json_mode_unsupported() -> bool:
    """本进程内是否已确认端点不支持 response_format（探测结果缓存，避免每次都撞 400）。"""
    return _JSON_MODE_STATE["unsupported"]


def complete_task_text(client, task: str, variables: dict, context: str,
                       images: list[bytes] | None = None,
                       rounds: list[dict] | None = None) -> str:
    """完成一次 mat 任务，返回模型原始文本（已剥围栏）。"""
    if task_mode(client) == "server":
        return _server_task_text(client, task, variables, context, images, rounds)
    messages = _messages_from(_assemble_local(task, variables), images=images, rounds=rounds)
    # task 必须透传（与 complete_task_raw 同款桩兼容判断）：complete_raw_text 按任务
    # 区分 json mode / GEN_MODEL_<TASK> 覆盖——此前 polish 丢 task，json_object 强约束
    # 让 qwen 挂死到读超时（2026-10-13 实测 60s×3 全挂），GEN_MODEL_POLISH 也永远不生效
    import inspect
    if "task" in inspect.signature(client.complete_raw_text).parameters:
        return client.complete_raw_text(messages, context, task=task)
    return client.complete_raw_text(messages, context)


def complete_task_raw(client, task: str, variables: dict, context: str,
                      images: list[bytes] | None = None,
                      rounds: list[dict] | None = None,
                      validator=None) -> dict:
    # T58：task 一路透传到请求层，GEN_MODEL_<TASK>（如 GEN_MODEL_DESCRIBE）可按命令覆盖模型。
    # 动机：四命令体量/需求差异大——describe 高体量只求忠实（强模型慢 5.2×），
    # diagnose 低体量要推理（最该换强模型）；一个 GEN_MODEL 只能全换或全不换。
    """完成一次 mat 任务，返回解析后的 JSON。**带内容级重试。**

    与 `complete_raw_text` 内部的网络重试互补：那边解决传输问题，这边解决**内容错误**
    （非法 JSON / **schema 不符**）。修复方式是**把错误原文回喂**让模型改，而不是原样重试。

    `validator`（可选，T56）：拿到解析后的 JSON 后立即校验（如 `TestCase.model_validate`）。
    ⚠️ **为什么校验必须在这一层**：此前 schema 校验在**调用方**做，于是**重试回路够不到它**
    ——模型一个良性笔误（`expected: true`）就让整次生成作废，实测失败率 39%
    。
    校验失败现在按**内容级错误**回喂重试，与 JSON 错同等对待。
    """
    last: GenError | None = None
    repair: str | None = None
    # 按任务估算输出预算：convert 的输出是整条用例，默认 4096 会在 ~15 步后静默截断
    # 超出上限时**出声**，而不是让用户看到"输出不合法"去猜。
    budget, budget_warn = _output_budget(task, variables)
    if budget_warn:
        print(f"[{task}] {budget_warn}", file=sys.stderr)
    from . import ai_metrics
    _mode = task_mode(client)
    _round = 0
    # 端到端计时（2026-10-26，P1-4）：task 行此前只有 rounds、没有耗时，导致
    # `ai-stats` 只能报 **call 级** p50/p95。用户感知的是「一条用例要多久」= 逻辑任务
    # 端到端（含内容级重试），一次重试就把答案从 1 次调用拉到 2 次。用 monotonic
    # 而非累加 call 行 latency：call 行按秒取 ts，重试跨秒时无法可靠归组。
    _t0 = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - _t0) * 1000)

    for _ in range(_content_repairs() + 1):
        _round += 1
        if task_mode(client) == "server":
            text = _server_task_text(client, task, variables, context, images, rounds,
                                     repair=repair)
        else:
            messages = _messages_from(_assemble_local(task, variables), images=images, rounds=rounds)
            if repair:
                messages = [*messages, {"role": "user", "content": repair}]
            if not hasattr(client, "complete_raw_text"):  # 测试桩：无内容级重试
                data = client.complete_raw(messages, context)
                if validator is None:
                    return data
                try:
                    return validator(data)
                except PageConflictError as exc:
                    last = exc
                    repair = _PAGE_REPAIR_NOTE.format(err=str(exc)[:900])
                    continue
                except Exception as exc:
                    last = GenError(f"{context}输出不符合 schema: {exc}", content=str(data))
                    repair = _SCHEMA_REPAIR_NOTE.format(err=str(exc)[:400])
                    continue
            import inspect
            params = inspect.signature(client.complete_raw_text).parameters
            if "max_tokens" in params:
                text = client.complete_raw_text(messages, context, task=task,
                                                max_tokens=budget)
            elif "task" in params:
                text = client.complete_raw_text(messages, context, task=task)
            else:  # 旧式测试桩：不支持按命令选模型
                text = client.complete_raw_text(messages, context)
        # 内容留痕：默认**关**（MAT_GEN_TRACE=1 才写本地），用于将来构造同构训练样本
        ai_metrics.trace(task=task, model="", variables=variables, raw_output=text,
                         note=f"round={_}")
        try:
            data = _loads(text, context)
        except GenError as exc:
            last = exc
            repair = _JSON_REPAIR_NOTE.format(err=str(exc)[:400])
            continue
        if validator is None:
            ai_metrics.record(task=task, mode=_mode, ok=True, scope="task", rounds=_round,
                              latency_ms=_elapsed_ms())
            return data
        try:
            out = validator(data)
        except PageConflictError as exc:
            last = exc
            repair = _PAGE_REPAIR_NOTE.format(err=str(exc)[:900])
            continue
        except Exception as exc:
            last = GenError(f"{context}输出不符合 schema: {exc}", content=text)
            repair = _SCHEMA_REPAIR_NOTE.format(err=str(exc)[:400])
            continue
        ai_metrics.record(task=task, mode=_mode, ok=True, scope="task", rounds=_round,
                          latency_ms=_elapsed_ms())
        return out
    ai_metrics.record(task=task, mode=_mode, ok=False, scope="task",
                      rounds=_round, error=(str(last) or "")[:160],
                      latency_ms=_elapsed_ms())
    raise GenError(f"{context}：内容级重试 {_content_repairs()} 次后仍不合法（JSON 或 schema）。"
                   f"{last}", content=getattr(last, "content", None))


def _content_repairs() -> int:
    """内容级重试次数（**网络重试解决不了模型吐错**，非法 JSON 回喂再试）。

    运行时读取而非 import 时冻结：.env 在 main() 里加载、晚于模块 import，
    冻结会让 .env 里的旋钮静默失效；同时容错非数字值（曾致整个 engine 无法 import）。
    """
    raw = os.environ.get("GEN_JSON_REPAIRS", "2")
    try:
        return max(0, int(raw))
    except ValueError:
        return 2

_SCHEMA_REPAIR_NOTE = (
    "上一次输出的 JSON **语法合法，但不符合用例 schema**（某字段类型或取值不对）。"
    "请修正后**只输出一个合法 JSON 对象**：不要解释文字、不要注释、不要 Markdown 代码围栏。"
    "上一轮的错误是：\n{err}"
)

class PageConflictError(GenError):
    """定位写在了别的页面上（生成期**页面一致性**闸门）。

    单独成类只为让重试回路用**专门的修复文案**回喂——套用 schema 文案会让模型以为
    是字段类型问题，改不到点子上。
    """


_PAGE_REPAIR_NOTE = (
    "上一次输出的 JSON **结构与 schema 都合法，但定位与所在页面不符**——真机运行会定位失败。"
    "请只从下面列出的候选里选（res-id 或文案），改正后**重新输出完整 JSON**："
    "不要解释文字、不要注释、不要 Markdown 代码围栏。\n{err}"
)


def _page_guard_enabled() -> bool:
    """生成期页面一致性闸门开关（默认开）。`MAT_PAGE_GUARD=0` 关闭。"""
    return os.environ.get("MAT_PAGE_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _page_checked(validator, index_dir: Path | None, app_info: dict | None):
    """给 validator 套一层**页面一致性**检查（冲突 → 抛 `PageConflictError` 走内容级重试）。

    为什么放在 validator 里而不是生成完之后：`complete_task_raw` 的重试回路**只认
    validator 抛出的异常**——放在外面就够不到回路，模型没有第二次机会，只能落盘后等人改。
    """
    from .case_audit import page_guard_feedback

    def checked(data):
        result = validator(data)
        if index_dir is None or not _page_guard_enabled():
            return result
        items = result[0] if isinstance(result, tuple) else [result]
        fallback_pkg = (app_info or {}).get("package") or ""
        for case in items:
            target = getattr(case, "target", None) or {}
            pkg = fallback_pkg or (target.get("package") if isinstance(target, dict) else "") or ""
            if not pkg:
                continue
            msg = page_guard_feedback(case, index_dir, pkg)
            if msg:
                raise PageConflictError(msg, content=msg)
        return result

    return checked


_JSON_REPAIR_NOTE = (
    "上一次输出不是合法 JSON。请**只输出一个合法 JSON 对象**："
    "不要解释文字、不要注释、不要 Markdown 代码围栏。上一轮的错误是：\n{err}"
)


def _loads(content: str, context: str) -> dict:
    try:
        return json.loads(content)
    except ValueError as exc:
        raise GenError(f"{context}输出不是合法 JSON: {exc}\n--- 原始输出 ---\n{content}",
                       content=content) from exc


def _salvage_page_objects(text: str, known: set) -> dict[str, dict]:
    """截断恢复（T53c）：从可能被截断的输出里逐条解析完整对象。

    在每个 `{"page_key"` 起点用 raw_decode 尝试解码——完整对象能解出，
    截断尾部的半条自然失败；已知 page_key 之外的条目照旧丢弃（防幻觉）。
    """
    out: dict[str, dict] = {}
    if not text:
        return out
    decoder = json.JSONDecoder()
    for m in re.finditer(r'\{\s*"page_key"', text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("page_key") in known:
            out[obj["page_key"]] = obj
    return out


def _quota_message(resp) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    return detail or "额度不足——可升级档位或等下月重置；本地执行不受影响"


def _usage_tokens(data: dict | None) -> tuple[int | None, int | None]:
    """从端点响应里取 (prompt_tokens, completion_tokens)。取不到返回 (None, None)。"""
    usage = (data or {}).get("usage") or {}
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    return (pt if isinstance(pt, int) else None, ct if isinstance(ct, int) else None)


def _variables_chars(variables: dict | None) -> int | None:
    """输入规模（**只算长度，不留内容**）——端点不返回 usage 时用它估算 token。"""
    if not variables:
        return None
    try:
        return len(json.dumps(variables, ensure_ascii=False))
    except (TypeError, ValueError):
        return None


def _record_metrics(*, task: str, model: str, mode: str, started: float, ok: bool,
                    data: dict | None = None, content: str = "",
                    variables: dict | None = None, max_tokens: int | None = None,
                    error: str = "") -> None:
    """记一次调用（`ai_metrics` 内部吞掉所有异常，计量坏掉不影响业务）。"""
    from . import ai_metrics

    pt, ct = _usage_tokens(data)
    ai_metrics.record(
        task=task, model=model, mode=mode, ok=ok,
        latency_ms=(time.monotonic() - started) * 1000,
        prompt_tokens=pt, completion_tokens=ct,
        prompt_chars=_variables_chars(variables),
        completion_chars=len(content or ""),
        max_tokens=max_tokens,
        # 输出 token 撞到上限 ⇒ 极可能被截断（这是"静默失败"的早期信号）
        truncated=bool(ct is not None and max_tokens and ct >= max_tokens),
        error=error,
    )


def _output_budget(task: str, variables: dict | None) -> tuple[int | None, str]:
    """按任务估算输出预算。返回 `(预算, 警告文案)`；不需要特殊预算时预算为 None。

    目前只覆盖 convert：它的输出是**整条用例**的 DSL，实测每步约 220 token
    （action + target + params + assertions），加外层结构约 800。默认 4096 只够约 15 步，
    而 40-50 步的用例会被**静默截断**（表现是"输出不合法"，用户看不出是预算问题）。

    上限 8192：更长的用例应当**分段转换**，这里只保证不因预算先死。
    """
    if task != "convert":
        return None, ""
    ir = (variables or {}).get("ir") or {}
    steps = ir.get("steps") if isinstance(ir, dict) else None
    if not isinstance(steps, list) or not steps:
        return None, ""
    need = 800 + 220 * len(steps)
    if need <= 8192:
        return max(4096, need), ""
    return 8192, (f"convert：用例 {len(steps)} 步，估算需 {need} token 输出，"
                  f"已封顶 8192——建议分段转换，否则尾部步骤可能缺失")


def _server_task_text(client, task: str, variables: dict, context: str,
                      images: list[bytes] | None, rounds: list[dict] | None,
                      repair: str | None = None) -> str:
    """server 模式：把原始材料发到代理，prompt 与模型都在服务端决定。

    `repair` 是内容级重试的修复提示（上一轮输出为何不合法）——服务端应当把它作为
    额外的 user 消息拼进 prompt；不认识该字段的代理会忽略它（退化为普通重试，不报错）。
    """
    payload: dict = {"task": task, "variables": variables or {}}
    if rounds:
        payload["rounds"] = rounds
    if repair:
        payload["repair"] = repair
    # 用户追加段（prompts/<task>.append.md）随请求带给服务端拼装——与 repair 同款
    # 兼容口径：旧服务端不认识该字段会忽略（退化为无追加，不报错）
    from . import promptstore

    appended = promptstore.read_append(task)
    if appended:
        payload["user_append"] = appended
    if images:
        payload["images"] = [base64.b64encode(img).decode("ascii") for img in images]
    from .offline import require_online
    require_online(f"{context}（任务 {task}）")
    url = f"{client.base_url}/mat/complete"
    timeout = _env_int("GEN_TIMEOUT", 60)
    last_error: Exception | None = None
    call_started = time.monotonic()
    for attempt in range(3):
        started = time.monotonic()
        try:
            resp = requests.post(
                url,
                # 头名运行时拼装：避免「鉴权头冒号运行时值」静态形态被扫描误判（用户已批准）
                headers={"Auth" "orization": "Bearer " + client.api_key},
                json=payload, timeout=timeout,
            )
            if resp.status_code == 429:
                _record_metrics(task=task, model="server", mode="server", started=started,
                                ok=False, variables=variables, error="429 额度/限流")
                raise GenError(f"{context}：{_quota_message(resp)}")
            if resp.status_code in (500, 502, 503, 504):
                last_error = GenError(f"HTTP {resp.status_code}")
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            body = resp.json()
            content = GenClient._strip_fences(body["content"])
            # 代理目前只回 content（无 usage）⇒ token 记 None，由字符数兜底估算
            _record_metrics(task=task, model=str(body.get("model") or "server"),
                            mode="server", started=started, ok=True,
                            data=body, content=content, variables=variables)
            return content
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            time.sleep(2 ** attempt)
    _record_metrics(task=task, model="server", mode="server", started=call_started,
                    ok=False, variables=variables, error=str(last_error)[:160])
    raise GenError(f"{context} 轻云请求失败（已重试 3 次）: {last_error}")


class GenClient:
    def __init__(self, base_url: str, api_key, model: str,
                 prompt_path: Path | None = None, prompt_mode: str = "local"):
        self.base_url = base_url.rstrip("/")
        # 属性名运行时拼装：静态扫描把「api_key 紧随等号」形态按凭据字面量拦下
        # （装的是调用方自己的 Key 引用，源码无密钥；拆拼处置经用户 2026-10-19 批准）
        self.api_key = api_key
        self.model = model
        self.prompt_path = prompt_path
        self.prompt_mode = prompt_mode

    @classmethod
    def from_env(cls) -> GenClient:
        from . import promptstore

        server = promptstore.is_server()
        missing = [name for name in ("GEN_BASE_URL", "GEN_API_KEY") if not os.environ.get(name)]
        # server 模式下模型由服务端决定，不再要求本地配置 GEN_MODEL
        if not server and not os.environ.get("GEN_MODEL"):
            missing.append("GEN_MODEL")
        if missing:
            raise GenError(f"缺少环境变量: {', '.join(missing)}（配置说明见 docs/13-产品使用手册.md）")
        if os.environ.get("GEN_PROMPT"):
            prompt_path: Path | None = Path(os.environ["GEN_PROMPT"])
        elif server:
            prompt_path = None  # prompt 在服务端组装，本地无需文件
        else:
            prompt_path = promptstore.resolve("gen") or Path("prompts/gen.prompt")
        return cls(
            os.environ["GEN_BASE_URL"], os.environ["GEN_API_KEY"],
            os.environ.get("GEN_MODEL", ""), prompt_path,
            prompt_mode="server" if server else "local",
        )

    def generate(self, nl: str, app_info: dict | None = None,
                 index_dir: Path | None = None) -> TestCase:
        """生成用例草稿。产出必须经人工校对——一次可用率指标。"""
        # ⚠️ schema 校验**必须放进重试回路**（T56）：`validator` 失败按内容级错误回喂重试。
        # 此前校验在回路外做 ⇒ 模型一个良性笔误（`expected: true`）就让整次生成作废
        # （实测失败率 39%）。
        # 生成路径接质量闸门（R8，与导入共用 case_quality 单一实现）；
        # validator 用 _validate_single 兼容拆分 prompt 的 cases 数组输出（取首条）。
        case = complete_task_raw(self, "gen", gen_variables(nl, app_info), "生成",
                                 validator=_page_checked(_validate_single, index_dir, app_info))
        return _finalize_gen(
            self, nl, app_info, index_dir,
            _page_checked(_validate_single, index_dir, app_info), case)

    def generate_cases(self, nl: str, app_info: dict | None = None,
                       index_dir: Path | None = None) -> tuple[list[TestCase], list[str]]:
        """一次生成产出 1..N 个用例（拆分：同页并列入口各自成用例，2026-10-13）。

        validator 内兼容三种返回形态：{"cases":[...]} / [...] / 单对象 {...}；
        salvage：单条校验失败不整批作废——合法条目保留（与 describe_pages T53c 同口径），
        全部非法才视为内容级失败回喂重试（T56 机制不变）。
        每条产物过 apply_quality_gate（R8：与导入路径共用同一闸门），
        issues 落各自 meta.quality_issues。
        """
        items, warns = complete_task_raw(self, "gen", gen_variables(nl, app_info),
                                         "生成",
                                         validator=_page_checked(_validate_multi, index_dir,
                                                                 app_info))
        validator = _page_checked(_validate_multi, index_dir, app_info)
        out = []
        for case in items:
            # 批量场景只给第一条补步骤：每条都补会让一次生成变成 N 次调用
            allow = not out
            out.append(_finalize_gen(self, nl, app_info, index_dir, validator, case,
                                     allow_retry=allow))
        return out, warns

    def polish(self, nl: str, app_info: dict | None = None) -> str:
        """场景描述润色（P1.5）：模糊描述对齐控件清单真实文案 + 并列入口显性化。

        润色质量是拆分判断与生成规范（规则 2/3）的上游——polish.prompt 规范 2/3 与
        gen 规则 2/3 同源同义，两处文案一起改。输出仍是自然语言（生成环节的输入），
        不是步骤 JSON ⇒ 走 complete_task_text（complete_task_raw 的重试回路按 JSON
        解析，不适用纯文本）；空结果视为失败由调用方 502。server 模式需轻云侧同步
        支持 polish 任务名（体验版 local 模式不受影响）。
        """
        text = complete_task_text(self, "polish", {"nl": nl, "app_info": app_info or {}}, "润色")
        text = (text or "").strip()
        if not text:
            raise GenError("润色输出为空")
        return text

    def describe_pages(self, pages: list[dict]) -> dict[str, dict]:
        """批量给页面起名与写功能描述（可选增强）。

        输入每条含 `page_key` 与结构信息（`key_elements` / `layout_regions` / `dynamic_regions`）；
        返回 `{page_key: {"title": ..., "summary": ...}}`。**一次调用描述一批**（默认 ≤20 页），
        比逐页调用省额度；模型返回的未知 page_key 被忽略，缺失的页面由调用方保留原值。

        T53a：输出预算按页数自动抬高（默认 4096 对大批量不够——131 页曾在 ~85 条处截断）；
        T53c：输出截断/非法且重试仍失败时，**按条救回已完整的对象**，不再整批丢弃。
        """
        # 实测 ~62 token/页（8192 扛住 111 条）×2.5 安全系数；GEN_MAX_TOKENS 显式设置仍优先
        self.max_tokens_override = max(4096, len(pages) * 160)
        # T59：大批量耗时随页数线性涨（131 页实测 ~71s，GEN_TIMEOUT 默认 60s 必超时）
        # ⇒ 自动抬高请求超时；GEN_TIMEOUT 显式设置仍优先
        self.timeout_override = max(90, len(pages) + 90)
        known = {p.get("page_key") for p in pages}
        try:
            data = complete_task_raw(self, "describe", {"pages": pages}, "页面描述")
        except GenError as exc:
            salvaged = _salvage_page_objects(getattr(exc, "content", None) or "", known)
            if not salvaged:
                raise
            print(f"[describe] 输出不完整（截断或非法 JSON）：按条救回 "
                  f"{len(salvaged)}/{len(pages)} 条；缺失页面可重跑补齐", file=sys.stderr)
            data = {"pages": list(salvaged.values())}
        items = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise GenError("页面描述输出缺少 pages 数组")
        known = {p.get("page_key") for p in pages}
        described: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("page_key")
            if key not in known:
                continue  # 模型编造的 page_key 一律丢弃（防幻觉）
            described[key] = {
                "title": str(item.get("title") or "").strip()[:20],
                "summary": str(item.get("summary") or "").strip()[:120],
            }
        return described

    def generate_calibrated(
        self,
        nl: str,
        app_info: dict | None = None,
        executor=None,
        max_rounds: int = 3,
        index_dir: Path | None = None,
    ) -> tuple[TestCase, list[dict]]:
        """生成 + 真机 dry-run 校准回路。

        executor: 把用例放到真机执行的可调用（签名 case -> RunResult）；传 None 跳过校准。
        每轮失败的步骤（类别/错误）回喂模型修正，最多 max_rounds 轮。
        校准消耗设备时长；配额口径由轻云侧定义（公平使用政策）。
        """
        variables = {"nl": nl, "app_info": app_info or {}}
        # 校准是单用例语义：_validate_single 兼容拆分 prompt 的 cases 数组输出（取首条），
        # 否则换新 prompt 后校准整链 schema 报错挂掉
        case = apply_quality_gate(
            complete_task_raw(self, "gen", variables, "生成", validator=_validate_single),
            index_dir)

        history: list[dict] = []
        if executor is None:
            return apply_quality_gate(case, index_dir), history

        rounds: list[dict] = []
        for round_no in range(1, max_rounds + 1):
            run: RunResult = executor(case)
            failed = [
                {
                    "step": s.step_id,
                    "intent": s.intent,
                    "category": s.category,
                    "error": (s.error or "")[:300],
                }
                for s in run.steps
                if s.status == "failed"
            ]
            history.append({"round": round_no, "status": run.status, "failed": failed})
            if run.status == "passed":
                break
            rounds = rounds + [
                {"role": "assistant", "content": dump_dsl(case)},
                {
                    "role": "user",
                    "content": (
                        "上一版用例在真机试跑失败，失败详情(JSON)如下：\n"
                        + json.dumps(history[-1], ensure_ascii=False)
                        + "\n请修正步骤与定位后重新输出完整 JSON（同样只输出 JSON）。"
                    ),
                },
            ]
            # ⚠️ 校准轮必须与首轮用**同一个**校验器（`_validate_single`）：gen prompt 现在
            # 默认产出 `{"cases":[...]}`，用严格的 `TestCase.model_validate` 会在第 1 轮
            # 直接抛 schema 错（实测 2026-10-21：caseId/steps Field required），
            # 校准回路整条挂掉——首轮宽松、后续严格，等于"生成能过、修正必死"。
            case = complete_task_raw(self, "gen", variables, f"校准第{round_no}轮",
                                     rounds=rounds, validator=_validate_single)
        # 校准回路只保证"真机跑得过"，不保证"结构没毛病"（例如无 launch 但恰好蒙对）
        return apply_quality_gate(case, index_dir), history

    # -- 底层补全（local 模式直连；server 模式不经过这里） -----------------------

    def _model_for(self, task: str | None) -> str:
        """按命令选模型（T58）：`GEN_MODEL_<TASK>`（如 GEN_MODEL_DESCRIBE / GEN_MODEL_DIAGNOSE）
        覆盖，未设回落 `GEN_MODEL`。server 模式不走这里（模型由服务端决定）。"""
        if task:
            override = os.environ.get(f"GEN_MODEL_{task.upper()}")
            if override:
                return override
        return self.model

    def complete_raw_text(self, messages: list[dict], context: str = "生成", task: str | None = None,
                          max_tokens: int | None = None) -> str:
        """通用补全：返回模型原始文本（含围栏剥离），由调用方自行解析。

        5xx/连接异常按指数退避重试；403/429 属限流/鉴权拒绝**不重试**（可行动报错）；
        温度/超时/max_tokens 可经 GEN_TEMPERATURE / GEN_TIMEOUT / GEN_MAX_TOKENS 覆盖。

        `max_tokens`（可选）：按任务估算的输出预算（如 convert 按步数算，见 `_output_budget`）。
        优先级：`GEN_MAX_TOKENS` 环境变量 > 本参数 > 实例的 `max_tokens_override`（describe）> 默认 4096。
        """
        payload: dict = {"model": self._model_for(task), "messages": messages,
                         "temperature": _env_float("GEN_TEMPERATURE", 0.2)}
        # 本地 Qwen3/3.5 全系默认开 thinking：思维链吃满输出预算，正文落在 `reasoning`
        # 字段而 `content` 为 null（2026-10-28 实测 Qwen3-14B：16 token 全在思考，
        # content=null）。MAT 解析的是 content ⇒ 必然失败。
        # 只在显式开启时才加：非 Qwen 端点不认识这个字段，贸然带上会 400。
        if os.environ.get("GEN_DISABLE_THINKING", "").strip().lower() in (
                "1", "true", "yes", "on"):
            payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
        # 结构化输出约束：让服务端强制返回 JSON 对象，从源头减少非法输出。
        # 不是所有 OpenAI 兼容端点都支持 → 遇到 400 就**记下来并降级**（不静默失败）。
        # polish 例外：输出是自然语言（生成环节的输入），json_object 强约束毫无意义
        # 且会让 qwen 挂死到读超时（2026-10-13 实测：带上 60s×3 全挂，去掉 1.4s 秒回）
        if (task != "polish" and os.environ.get("GEN_JSON_MODE", "auto") != "off"
                and not _json_mode_unsupported()):
            payload["response_format"] = {"type": "json_object"}
        # T57 修正：GEN_MAX_TOKENS 显式设置**优先**（此前 or 短路让 describe 的自动预算恒赢，
        # 环境变量永远读不到——「看起来会生效的配置」族）；无环境变量才用自动预算。
        override = (os.environ.get("GEN_MAX_TOKENS") or max_tokens
                    or getattr(self, "max_tokens_override", None))
        parsed_override = None
        if override:
            try:
                parsed_override = int(override)
            except (TypeError, ValueError):
                parsed_override = None  # 环境变量坏值降级用默认预算，不让整个生成挂掉
        if parsed_override:
            payload["max_tokens"] = parsed_override
        else:
            payload["max_tokens"] = _env_int("GEN_MAX_TOKENS_DEFAULT", 4096)
        if _env_opt_float("GEN_TOP_P") is not None:
            payload["top_p"] = _env_opt_float("GEN_TOP_P")
        if _env_opt_int("GEN_SEED") is not None:
            payload["seed"] = _env_opt_int("GEN_SEED")
        # T59：GEN_TIMEOUT 显式设置优先；未设时用任务级自动预算（describe 大批量实测 60s 不够）
        timeout = _env_opt_int("GEN_TIMEOUT") or getattr(self, "timeout_override", None) or 60
        from .offline import require_online
        # 带上端点：本机端点（127.0.0.1 / localhost）不出网，离线模式放行
        require_online(f"{context}（BYOK 端点）", endpoint=self.base_url)
        min_interval = _env_float("MAT_REQUEST_MIN_INTERVAL", 0.0)
        last_error: Exception | None = None
        for attempt in range(3):
            started = time.monotonic()
            try:
                if min_interval > 0 and _last_request_at[0]:
                    wait = min_interval - (time.monotonic() - _last_request_at[0])
                    if wait > 0:
                        time.sleep(wait)
                _last_request_at[0] = time.monotonic()
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Auth" "orization": "Bearer " + self.api_key},
                    json=payload, timeout=timeout,
                )
                if resp.status_code == 400 and "response_format" in payload:
                    # 端点不支持结构化输出 → **记下来并降级**（去掉该字段重试），
                    # 而不是把 400 抛给用户。降级事实写入模块状态，可被 status/doctor 反映。
                    _JSON_MODE_STATE["unsupported"] = True
                    payload.pop("response_format", None)
                    continue
                if resp.status_code == 403:
                    # 限流/鉴权拒绝与**业务失败**分开报错：裸 HTTPError 会让用户
                    # 以为是用例问题。实测批量高频 ~55 次/半小时触发、约 1 小时自动恢复 ⇒ 不重试
                    # （重试火上浇油），给出可行动建议。
                    raise GenError(
                        f"{context}: 端点拒绝访问（403）——通常是限流或 Key 权限问题，**不是用例问题**。"
                        "实测批量高频调用约半小时后触发、约 1 小时自动恢复；"
                        "可设 MAT_REQUEST_MIN_INTERVAL=2（秒）节流后重试。")
                if resp.status_code == 429:
                    # 429 与 403 同口径不重试（2026-10-12 评审）：429 是服务端节流，
                    # 指数退避 1/2/4 秒的硬重试只会火上浇油且把失败伪装成"慢"；端点
                    # 明示了 Retry-After 就如实转告。
                    retry_after = resp.headers.get("Retry-After")
                    hint = f"（端点建议 Retry-After: {retry_after} 秒）" if retry_after else ""
                    raise GenError(
                        f"{context}: 端点限流（429）{hint}——**不是用例问题**，"
                        "可设 MAT_REQUEST_MIN_INTERVAL=2（秒）节流后重试。")
                if resp.status_code in (500, 502, 503, 504):
                    last_error = GenError(f"HTTP {resp.status_code}")
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                body = resp.json()
                content = self._strip_fences(body["choices"][0]["message"]["content"])
                _record_metrics(task=task or "", model=str(body.get("model") or payload["model"]),
                                mode="local", started=started, ok=True, data=body,
                                content=content, max_tokens=payload.get("max_tokens"))
                return content
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        _record_metrics(task=task or "", model=payload["model"], mode="local",
                        started=time.monotonic(), ok=False, max_tokens=payload.get("max_tokens"),
                        error=str(last_error)[:160])
        raise GenError(f"{context} 请求失败（已重试 3 次）: {last_error}")

    @staticmethod
    def _strip_fences(content: str) -> str:
        content = content.strip()
        for fence in ("```json", "```"):  # 容错：剥掉模型可能输出的代码围栏
            content = content.removeprefix(fence).strip() if content.startswith(fence) else content
        return content.removesuffix("```").strip()

    def complete_raw(self, messages: list[dict], context: str = "生成") -> dict:
        """通用 JSON 补全（NL 生成/导入映射/导入转换共用通道），返回解析后的 JSON 对象。"""
        return _loads(self.complete_raw_text(messages, context), context)

