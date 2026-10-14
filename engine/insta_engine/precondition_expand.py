"""把「可置备的页面状态」前置条件展开成**导航步骤**（R9b）。

## 问题

手工用例常写「前置条件：已进入搜索结果页」——人工测试时可以自己先点进去，
但自动化用例必须**自己走到那儿**。实测：xlsx 批次 8 条声明了这类前置条件的用例，
跑测**全部照红**（locate-fail）——因为 MAT 的动作集改不了页面状态，
「声明了」不等于「有人会去置备它」。

## 为什么复用 `gen` 任务而不新增 prompt

  · `gen` 的 prompt 已经有**反幻觉规则**（只用控件清单里真实存在的文案或 res-id，
    禁止编造）与**「并列入口拆分/`back`」规则** —— 正是导航步骤最容易错的两处；
  · 不新增 prompt 资产 ⇒ 不用改 `promptstore.NAMES` / `ai_client.TASKS` / 服务端组装，
    本地与服务端两种模式都直接可用；
  · 额度口径复用现有 `gen`（0.5 次/条），不新增计费线。

⚠️ **代价是要适配 `gen` 的输出契约**（`{"cases":[...]}` 数组）与它的"测试用例"取向 ——
措辞必须写成**一个正常的测试场景**，否则模型会拒答（实测：写成"只做一件事…"这类
命令式指令时，同一次返回过 `{"cases": []}`）。见 `_loose` 与 `_PROMPT` 的注释。

## 只展开 ③（可置备页面状态）

① 环境前提不用展开（它是运行前提）；② 系统权限态**展开不了**（动作集改不了权限，已拒跑）；
④ 随机事件**不该展开**（"导航到一个开屏广告"是荒谬的）。见 `preconditions.classify`。

## 展开结果**必须可人工复核**

插进去的步骤 `intent` 带 `【前置展开】` 前缀，并在 issue 里写明"已展开 N 步，请人工确认"，
`meta.expanded_preconditions` 留档。**理由**：模型可能给错路径，而错路径比不展开更难查。
"""

from __future__ import annotations

import os

__all__ = ["expand", "expand_enabled"]

#: 允许出现在展开结果里的动作（与 `dsl` 的动作集一致）。
ACTIONS = ("launch", "tap", "input", "clear", "swipe", "scroll", "back", "home",
           "key-event", "wait")

#: 展开出来的步骤的 intent 前缀 —— 人工复核时的抓手（见模块说明）。
MARK = "【前置展开】"

_PROMPT = """测试场景：验证 App 能进入「{target}」状态。
请只给出**从冷启动到该状态**的操作步骤，最后停在该状态即可。

要求：
1. 到达该状态**之后**的任何操作都不要包含（后续步骤由原用例负责）
2. 步骤里**不要加断言**（断言由原用例负责）
3. 定位只用「App 信息」里真实存在的控件文案或 res-id；禁止编造
4. 如果你**无法确定**到达该状态的路径，就返回空的 `steps`（**不要瞎猜** ——
   猜错的路径比不展开更难查）
"""


def expand_enabled() -> bool:
    """是否允许展开（默认开）。`MAT_EXPAND_PRECONDITIONS=0` 关闭。"""
    return os.environ.get("MAT_EXPAND_PRECONDITIONS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _loose(data) -> dict:
    """宽松校验：兼容 `gen` 当前的 `{"cases":[...]}` 契约，并**允许 `steps: []`**。

    ⚠️ 两点都是实测逼出来的：

    · **兼容 `{"cases":[...]}`**：`gen` prompt 的输出契约是数组（另一会话 2026-10-13
      改成拆分生成）。写成"顶层 `steps`"会在真机上直接失败 —— 实测第一次调用就是
      这样报的 `缺少 steps`。**不要照抄 `.example` 推断线上契约，跑一次就知道**。
    · **允许空 `steps`**：`ai_client._validate_single` 要求至少 1 步，而
      "无法确定路径 ⇒ 空"对展开来说是**合法答案**；用严格校验器会把"我做不到"
      当成 schema 错误回喂重试，把模型**逼着编一条路径**出来。
      ⚠️ 实测模型**会偶发拒答**（同一输入一次返回 `{"cases": []}`、另一次返回 2 步）
      ⇒ 空结果必须走优雅降级，不能当异常。
    """
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        data = data["cases"][0] if data["cases"] else {"steps": []}
    if not isinstance(data, dict):
        raise ValueError("输出必须是 JSON 对象")
    steps = data.get("steps")
    if steps is None:
        raise ValueError("缺少 steps")
    if not isinstance(steps, list):
        raise ValueError("steps 必须是数组")
    return data


def _normalize(data: dict) -> tuple[list[dict], list[str]]:
    """把模型输出规整成可插入的步骤（丢掉 launch / 非法动作 / 断言）。

    ⚠️ **不在这里分配 `id`**：唯一性只有看到整条用例才知道 ⇒ 由插入方
    （`manual_import._insert_after_launch`）分配，避免与既有 id 撞车。
    """
    out: list[dict] = []
    notes: list[str] = []
    for raw in data.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip()
        if action == "launch":
            continue  # 原用例已有 launch，不重复
        if action not in ACTIONS:
            notes.append(f"展开结果里的动作 `{action}` 不在动作集内，已丢弃")
            continue
        target = raw.get("target") if isinstance(raw.get("target"), dict) else None
        intent = str(raw.get("intent") or "").strip()
        out.append({
            "action": action,
            "target": target,
            "params": raw.get("params") if isinstance(raw.get("params"), dict) else {},
            "intent": f"{MARK}{intent}" if intent else MARK.rstrip(),
            "assertions": [],  # 展开只负责导航，断言归原用例
        })
    return out, notes


def expand(client, items: list[str], package: str, controls: str = "",
           graph: str = "") -> tuple[list[dict], list[str], bool]:
    """把 ③ 类前置条件展开成导航步骤。**永不抛出**（失败降级成"不展开 + 说明"）。

    返回 `(steps, notes, called)`：
      · `steps` 为空表示没展开（模型判定无法确定路径，或调用失败）；
      · `called` 表示**是否真的发起了模型调用** —— 额度记账要用它，
        否则"没发出去也扣钱"（用户会莫名其妙少额度）。
    """
    targets = [str(i).strip() for i in items if str(i).strip()]
    if not targets:
        return [], [], False
    from .ai_client import complete_task_raw

    app_info: dict = {"package": package}
    if controls:
        app_info["controls"] = controls
    if graph:
        app_info["graph"] = graph
    called = False
    try:
        called = True  # 进入调用路径即算"发起了"（失败也可能已被计费）
        data = complete_task_raw(
            client, "gen",
            {"nl": _PROMPT.format(target="；".join(targets)), "app_info": app_info},
            "前置条件展开", validator=_loose,
        )
    except Exception as exc:  # 展开失败不该让整条用例转换失败
        return [], [f"前置条件展开失败（{type(exc).__name__}: {exc}）："
                    "该用例仍按「未展开」处理，请在执行前手工置备"], called
    steps, notes = _normalize(data)
    notes += [str(i) for i in (data.get("issues") or []) if str(i).strip()]
    return steps, notes, called
