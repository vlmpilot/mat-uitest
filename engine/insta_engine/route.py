"""生成请求走哪一侧：本站自部署的模型（默认）还是云端模型服务商。

产品决策（2026-11-02）：**默认自部署**，只有显式 `MAT_ROUTE=cloud` 才走云端。

为什么默认值才是真决策：测试工程师装工具时想的是"能不能跑通"，不会去判断
"我的控件文本算不算敏感"；真正有隐私要求的（金融、政企、接甲方项目）也不会
主动翻文档找一个开关。**大多数人不表态 ⇒ 默认值就是事实标准。**

三种取值：

| `MAT_ROUTE` | 行为 |
|---|---|
| 未设 / `auto` | 按服务端策略：能自部署就自部署（free/pro/team），private 走云端 |
| `local` | **强制**自部署；端点缺失或不可用一律失败，**不静默改走云端** |
| `cloud` | 强制云端模型服务商（质量优先；文本与截图都会发给模型商） |

`local` 之所以要"失败而不回落"：选了它的人就是为了"内容别出去"，而回落恰好
发生在自部署不可用（Mac 睡眠、断网）这种最不该静默的时刻。宁可让这一次生成
失败并说清原因，也不能把截图悄悄发出去。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

#: 环境变量名（工作区 `.env` 或进程环境；`.env` 由 `main()` / MCP / 控制台加载）。
ENV_KEY = "MAT_ROUTE"

AUTO = "auto"
LOCAL = "local"
CLOUD = "cloud"
#: 只在 `mat status` 内部使用：云端任务但服务端没配云端（调用会报错）。
BLOCKED = "blocked"

#: 认得的取值。**不在这里的一律按 auto 处理**——拼错（`clod`）走的是安全方向
#: （自部署、不外发），并由 `mat doctor` 提示"这个值不认识"。
_KNOWN = {"": AUTO, "auto": AUTO, "local": LOCAL, "cloud": CLOUD}


def raw() -> str:
    """环境里的原始值（`doctor` 用来提示拼写错误）。"""
    return (os.environ.get(ENV_KEY) or "").strip().lower()


def preference() -> str:
    """规范化后的取值：`auto` / `local` / `cloud`。"""
    return _KNOWN.get(raw(), AUTO)


def is_cloud() -> bool:
    return preference() == CLOUD


def is_local() -> bool:
    return preference() == LOCAL


def is_explicit() -> bool:
    """用户是否**显式**表过态（未设 / auto 时为假，此时不带请求头）。"""
    return preference() != AUTO


def unknown_value() -> str:
    """设了但不认识的值（否则空串）——给 `doctor` 报出来。"""
    value = raw()
    return value if value and value not in _KNOWN else ""


# -- 显式策略表（`MAT_ROUTE_POLICY`，2026-11-04） ------------------------------
#
# 为什么要有第二层开关：`MAT_ROUTE` 是**单值**（local / cloud / auto），表达不了
# "视觉走本地、describe 走云端、简单 gen 走本地"这种按任务分流。策略表只做**决策与
# 观测**，不改变默认值：默认 `legacy` = 与引入前逐字节一致（不带 X-Mat-Route，
# 仍由服务端 auto 决定）。只有显式设 `MAT_ROUTE_POLICY=explicit` 才按表分流，
# 且 `MAT_ROUTE` 仍然**优先级最高**（运维兜底不能被策略表盖掉）。

ENV_POLICY = "MAT_ROUTE_POLICY"
LEGACY = "legacy"
EXPLICIT = "explicit"

#: 任务 → 固定去向。这张表是**唯一**的路由规则来源（测试直接读它，避免文档与实现漂移）。
#: `vision-locate` 恒定本地（截图最敏感、实测 1.4s vs 云端 2.6s；也是本地**唯一必须
#:   常驻**的模型——文本任务都有云端等价物，视觉兜底没有，去掉它就无法定位）；
#: `gen` / `convert` 恒定云端（2026-11-04 用户决定：结构化生成/转换不按体量分流，
#:   小体量的本地收益抵不过质量风险，统一保云端质量天花板）；
#: `describe` 输入 13–17k token、输出 2.7k token，本地解码慢于云端 ⇒ 云端；
#: `diagnose` 低体量但要推理 ⇒ 云端；
#: `map` / `polish` 已核实为**纯文本**小任务（variables 只有 headers/rows、nl/app_info，
#:   请求不带 images）⇒ 2026-11-04 起也走云端：本地文本模型因此可以整体下掉，
#:   服务器只需常驻 VL。整机离线/自带 Key 仍可用 `MAT_ROUTE=local` 强制本地
#:   （优先级最高），但要求部署方自己起文本服务——**不再有按任务开关**。
POLICY_TABLE: dict[str, str] = {
    "vision-locate": "local",
    "describe": "cloud",
    "diagnose": "cloud",
    "gen": "cloud",
    "convert": "cloud",
    "map": "cloud",
    "polish": "cloud",
}

#: prompt 模板自身占的字符（服务端私有模板约 3.9k；公开 example 11k）。估算时
#: 先加这一段，否则只算 variables 会把 prompt token 系统性低估 30%–60%。
#: 现在**只用于观测**（`route_est_tokens`），不再参与去向判断。
TEMPLATE_CHARS: dict[str, int] = {
    "gen": 4000,
    "convert": 4000,
    "map": 600,
    "polish": 1300,
    "describe": 2500,
    "diagnose": 2300,
    "vision-locate": 800,
}

#: 字符 → token 的启发式（与 `ai_metrics` 文档里的 chars/1.6 同口径）。
CHARS_PER_TOKEN = 1.6
# 说明：`MAT_ROUTE_TOKEN_THRESHOLD` / 8000 token 阈值随 "gen/convert 一律云端"
# 一起**移除**。它们只存在于本批未提交的改动里，尚未对外发布，没有环境变量依赖，
# 留着会让人误以为还能按体量分流。

#: `mat status` 展示的任务顺序（先最敏感的视觉，再文本主链路）。
TASK_ORDER: tuple[str, ...] = (
    "vision-locate", "gen", "convert", "describe", "diagnose", "map", "polish",
)

#: 文本任务：只受文本端点/策略表影响；vision-locate 单独看视觉端点。
TEXT_TASKS: tuple[str, ...] = tuple(t for t in TASK_ORDER if t != "vision-locate")


@dataclass(frozen=True)
class Decision:
    """一次路由决策（**只读**，供请求头与计量使用）。"""

    task: str
    route: str            # local / cloud / auto
    reason: str           # 可读原因，落进 ai.jsonl
    est_tokens: int | None
    round_index: int

    @property
    def is_explicit(self) -> bool:
        return self.route in (LOCAL, CLOUD)


def policy() -> str:
    """`MAT_ROUTE_POLICY`：默认 `legacy`；不认识的值也按 legacy（= 不改行为）。"""
    value = (os.environ.get(ENV_POLICY) or "").strip().lower()
    return EXPLICIT if value == EXPLICIT else LEGACY


def is_policy_explicit() -> bool:
    return policy() == EXPLICIT


def estimate_chars(variables: dict | None) -> int:
    """variables 的 JSON 字符数（只算长度，不留内容）。"""
    try:
        return len(json.dumps(variables or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def estimate_tokens(task: str, variables: dict | None = None) -> int:
    """估算 prompt token：`(variables 字符 + 模板字符) / 1.6` 向上取整。

    **只用于观测**（`route_est_tokens`）：去向由任务决定，不再按体量分流；
    保留它是为了 A/B 时能直接看出"这条请求大概多重"，以及后续若要恢复体量分流
    有一个可测的单点。
    """
    chars = estimate_chars(variables) + TEMPLATE_CHARS.get(task, 0)
    return int(math.ceil(chars / CHARS_PER_TOKEN)) if chars > 0 else 0


def decide(task: str, variables: dict | None = None, *,
           round_index: int = 1) -> Decision:
    """按优先级给出本次去向：`MAT_ROUTE` 显式值 > 策略表 > auto（服务端决定）。

    优先级（有测试钉住）：
      1. `MAT_ROUTE=local|cloud` —— 运维兜底，压过策略表；
      2. `MAT_ROUTE_POLICY != explicit` —— `auto`，**与现状完全一致**；
      3. `vision-locate` —— 恒定 local（截图不外发，重试也不改道）；
      4. 第 2 轮及以后 —— cloud（同一产物不混源）；
      5. 任务固定去向（`gen` / `convert` / `describe` / `diagnose` / `map` /
         `polish` 恒定 cloud，不再看 token；`MAT_ROUTE=local` 是唯一强制本地的方式）。
    """
    est = estimate_tokens(task, variables)
    round_no = max(1, int(round_index or 1))

    override = preference()
    if is_explicit():
        return Decision(task, override, f"env override: {ENV_KEY}={override}", est, round_no)

    if not is_policy_explicit():
        return Decision(task, AUTO, "policy:legacy", est, round_no)

    if task == "vision-locate":
        return Decision(task, LOCAL, "policy:vision-locate→local", est, round_no)

    if round_no >= 2:
        return Decision(task, CLOUD, f"policy:round {round_no}→cloud", est, round_no)

    rule = POLICY_TABLE.get(task)
    if rule == "local":
        return Decision(task, LOCAL, f"policy:{task}→local", est, round_no)
    if rule == "cloud":
        return Decision(task, CLOUD, f"policy:{task}→cloud", est, round_no)
    return Decision(task, AUTO, "policy:unknown-task→auto", est, round_no)


def route_map(remote_routing: dict | None = None) -> dict[str, str]:
    """每个任务**当前**去向（`local` / `cloud` / `auto` / `blocked`）——status 与测试共用。

    来源与 `decide()` 对齐：
      1. `MAT_ROUTE` 显式值：所有任务按它（含 vision）；
      2. `MAT_ROUTE_POLICY=explicit`：`decide()` 按任务表；
      3. `legacy`：`decide()` 返回 `auto`，而服务端 auto 的实际规则是**本地优先**
         （配了本地端点就走本地；本地不可用即报错，**不回落、也不按轮次改道**）；
         只有**没配**该端点时才走云端。这里用 `/usage.routing` 的两个事实把 auto
         落实成 local / cloud；2026-11-05 起服务端上报的是**实际去向**（档位 + 端点
         配置，见 `core.py:_routing_facts`）。更老的服务端该字段只按档位算，无法
         区分——遇到时先升级服务端；完全没上报则保持 auto（未确认）。
    - `cloud_available=False`（无凭证/无云端端点）：云端任务标记 `blocked`，调用会报错。
    """
    facts = remote_routing or {}
    routes = {task: decide(task).route for task in TASK_ORDER}

    if not is_explicit() and not is_policy_explicit():
        for task in TASK_ORDER:
            flag = facts.get("vision_local" if task == "vision-locate" else "text_local")
            if flag is True:
                routes[task] = LOCAL
            elif flag is False:
                routes[task] = CLOUD

    if facts.get("cloud_available") is False:
        for task, value in routes.items():
            if value == CLOUD:
                routes[task] = BLOCKED
    return routes


def describe_route(remote_routing: dict | None = None) -> list[str]:
    """`mat status` 用的**任务粒度**路由说明（只读，不打印、不读环境以外的状态）。

    去向一律经 `route_map()` → `decide()` / `/usage.routing` 推导，不硬编码字符串：
    - `legacy`：服务端 auto 是**本地优先**，本地不可用即报错；没配本地端点才走云端。
      `decide()` 不按轮次改道（它恒返回 auto），所以不能写成"第 2 轮起云端"。
    - `explicit`：按 `POLICY_TABLE`；`decide()` 的"第 2 轮起 cloud"对当前表无额外影响
      （文本任务首轮已是云端），vision-locate 不受轮次影响。
    - `MAT_ROUTE=local|cloud`：覆盖以上全部。

    文案刻意写成"当前去向"而不是承诺句；任何"整体级"结论都必须与服务端事实上报一致。
    """
    facts = remote_routing or {}
    text_local = facts.get("text_local")
    vision_local = facts.get("vision_local")
    cloud_available = facts.get("cloud_available")
    routes = route_map(facts)
    pref = preference()

    if is_explicit():
        head = f"当前去向（{ENV_KEY}={pref}，覆盖策略表）："
    elif is_policy_explicit():
        head = f"当前去向（策略表 {EXPLICIT}，按任务）："
    else:
        head = f"当前去向（默认 {LEGACY}：服务端 auto = 本地优先，不按轮次改道）："

    lines = [head]
    for label, value in (
        ("本地（不可用即报错）", LOCAL),
        ("云端（内容会发出去）", CLOUD),
        ("服务端决定（未确认）", AUTO),
        ("不可用（调用会报错）", BLOCKED),
    ):
        tasks = [task for task in TASK_ORDER if routes[task] == value]
        if tasks:
            lines.append(f"  - {label}：{'、'.join(tasks)}")

    if "text_local" in facts or "vision_local" in facts:
        lines.append(
            "  服务端事实：文本走本地="
            + ("是" if text_local else "否")
            + " · 视觉走本地="
            + ("是" if vision_local else "否")
            + "（按档位 + 端点配置判定，不是承诺；健康不参与此判定）"
        )

    if is_explicit() and pref == CLOUD:
        if cloud_available is False:
            lines.append("  说明：全部强制云端，但服务端没有可用的云端配置——调用会报错。")
        else:
            lines.append("  说明：全部强制云端——文本与截图都会发给模型服务商。")
    elif is_explicit() and pref == LOCAL:
        if text_local is False or vision_local is False:
            lines.append("  说明：MAT_ROUTE=local 但服务端不支持对应的本地端点——"
                         "相关请求会被拒绝，不会改走云端。")
        else:
            lines.append("  说明：全部强制本地——本地不可用会直接报错，不会改走云端。")
    elif is_policy_explicit():
        # 文本任务全在云端，但"云端不可用"时它们会是 `blocked`——这时不能再说
        # "会发出"（2026-11-04 补）：分组写着"不可用（调用会报错）"、说明却说
        # 内容发出去了，与 legacy/`MAT_ROUTE=cloud` 两个分支的写法也不一致。
        # 视觉尾巴同理：没有本地视觉端点时那条请求会被拒绝，
        # 不能留一句"截图仍在本地"让人以为视觉兜底还能用。
        vision_tail = ("vision-locate 的截图仍在本地。" if vision_local is not False
                       else "vision-locate 无法在本站处理（见下）。")
        if any(routes[task] == BLOCKED for task in TEXT_TASKS):
            lines.append("  说明：文本任务按策略表走云端，但服务端没有可用的云端配置——"
                         f"调用会报错，内容不会发出；{vision_tail}")
        else:
            lines.append("  说明：文本任务首轮即走云端，会发出场景描述、控件清单与导入样本行；"
                         f"{vision_tail}")
        lines.append("  说明：decide() 的「第 2 轮起强制 cloud」对当前策略表无额外影响"
                     "（文本任务首轮已是云端）；vision-locate 不受轮次影响。")
        if vision_local is False:
            # 与 `MAT_ROUTE=local` 同一事实：`X-Mat-Route: local` 到服务端后由
            # `_require_local` 校验配置，缺端点直接 400（core.py:326-328），
            # 客户端没有改道云端的分支 ⇒ 截图不会外发，只会失败。
            lines.append("  说明：服务端没有本地视觉端点，vision-locate 请求会被拒绝"
                         "（400，截图不会外发）——去掉该策略或改用自带 API Key 才有视觉兜底。")
    elif not facts:
        lines.append("  说明：服务端没有上报路由事实（老版本），实际去向由服务端 auto "
                     "决定，无法确认。")
    else:
        # legacy + 服务端事实：整体结论必须与上面的任务分组一致。
        if text_local and vision_local:
            lines.append("  说明：文本与截图都优先在本地处理；本地不可用会直接报错，"
                         "不会改走云端。")
        elif text_local and not vision_local:
            lines.append("  说明：文本优先在本地处理；视觉没有本地端点——"
                         "vision-locate 的截图会发给模型服务商。")
        elif not text_local and vision_local:
            lines.append("  说明：文本没有本地端点，文本任务会发给模型服务商"
                         "（配置如此，不是故障回落）；截图仍在本地。")
        else:
            lines.append("  说明：文本与视觉都没有本地端点，相关请求会发给模型服务商"
                         "（配置如此，不是故障回落）。")
        if cloud_available is False:
            if any(routes[task] in (CLOUD, BLOCKED) for task in TASK_ORDER):
                lines.append("  说明：服务端没有可用的云端配置——标为云端/不可用的任务会报错。")
            else:
                lines.append("  说明：未配置云端——只能走本地；本地不可用会直接报错。")
        # 「档位说本地、端点没配」是**配置矛盾**，不能静默挑一个答案（2026-11-05）：
        # 组里的去向写云端是事实，这里补一句为什么会这样，否则用户会以为档位没生效。
        if facts.get("text_conflict") is True:
            lines.append("  矛盾：该档位声明文本在本地处理，但服务端未配置文本端点"
                         "（MAT_LOCAL_BASE_URL）——文本实际会发给模型服务商。"
                         "请补端点，或显式设 MAT_ROUTE=cloud 并写清口径。")
    return lines
