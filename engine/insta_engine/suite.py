"""用例/套件加载：`run` 命令的入口解析（单测覆盖）。

套件 JSON（两种写法，均支持）：
- 平铺：{"name": "...", "on_failure": "continue|abort", "cases": ["cases/a.json", {...内联...}]}
- 分阶段：{"name": "...", "stages": [{"name": "setup", "on_failure": "abort", "cases": [...]}, ...]}
条目（字符串）也可以指向**另一个套件文件** ⇒ 递归展开（最多 3 层，防循环）。

单用例 JSON: TestCase 本体（含 "steps"）。

共享数据集（2026-10-21）：用例 `datasets[].ref`（如 `data/accounts.json#vip`）在加载期
解析成 vars —— 见 `resolve_dataset_refs`。解析失败**直接报错**，不静默留空：带着空数据
跑到一半才红，排查会从"数据没准备"跑偏到"定位/断言有问题"。
"""

from __future__ import annotations

import json
from pathlib import Path

from .dsl import TestCase

#: 套件嵌套上限（防 A→B→A 这类循环引用把 `mat run` 卡死）
MAX_SUITE_NEST = 3


def is_suite_payload(data: object) -> bool:
    """判套件还是单用例：有 cases/stages、且没有 steps。"""
    return (
        isinstance(data, dict)
        and ("cases" in data or "stages" in data)
        and "steps" not in data
    )


def load_case_or_suite(path: Path) -> tuple[str, object]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if is_suite_payload(data):
        return "suite", data
    return "case", TestCase.model_validate(data)


def resolve_dataset_refs(case: TestCase, base_dir: Path) -> TestCase:
    """共享数据集：把 `datasets[].ref` 解析成 vars（2026-10-21 最小闭环）。

    `ref` 形如 `data/accounts.json` 或 `data/accounts.json#vip`（#后是锚点键，取该键下的对象）。
    查找顺序：用例/套件所在目录 → `<cwd>/data/` → `<cwd>/`。

    - 数据文件里的键值先铺底，条目自带的 `vars` 覆盖它（就近优先）；
    - 文件缺失 / JSON 非法 / 锚点不存在或不是对象 ⇒ `ValueError`（fail-loud）。
    """
    if not case.datasets:
        return case
    resolved: list[dict] = []
    for idx, ds in enumerate(case.datasets, 1):
        if not isinstance(ds, dict):
            raise ValueError(f"datasets[{idx}] 必须是对象，收到 {type(ds).__name__}")
        ref = str(ds.get("ref") or "").strip()
        if not ref:
            resolved.append(ds)
            continue
        rel, _, anchor = ref.partition("#")
        candidates = [
            Path(base_dir) / rel,
            Path.cwd() / "data" / rel,
            Path.cwd() / rel,
        ]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            joined = "、".join(str(p) for p in candidates)
            raise ValueError(f"datasets[{idx}].ref 指向的文件不存在: {ref}（查过: {joined}）")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"datasets[{idx}].ref 不是合法 JSON: {path} — {exc}") from exc
        if anchor:
            node = data.get(anchor) if isinstance(data, dict) else None
            if not isinstance(node, dict):
                raise ValueError(f"datasets[{idx}].ref 锚点不存在或不是对象: {ref}")
            data = node
        if not isinstance(data, dict):
            raise ValueError(f"datasets[{idx}].ref 内容必须是键值对象: {ref}")
        entry = {k: v for k, v in ds.items() if k != "ref"}
        # 变量只接受字符串（与 TestCase.vars 同口径）：数字/布尔统一转字符串，避免下游替换类型漂移
        entry["vars"] = {**{str(k): str(v) for k, v in data.items()},
                         **(ds.get("vars") or {})}
        resolved.append(entry)
    return case.model_copy(update={"datasets": resolved})


def expand_suite(
    payload: dict, base_dir: Path, *, depth: int = 0
) -> list[tuple[TestCase, str, str, str]]:
    """把套件展开成 [(用例, 产物名, 阶段名, on_failure)]。

    - 平铺写法（只有 cases）⇒ 单阶段，阶段名空串，gate 取顶层 on_failure（默认 continue）；
    - stages 写法 ⇒ 逐阶段展开，gate 取阶段自身的 on_failure；
    - 条目指向另一个套件 ⇒ 递归展开，阶段名拼成 `父/子`（嵌套以子套件自身的 gate 为准）。
    """
    if depth > MAX_SUITE_NEST:
        raise ValueError(f"套件嵌套超过 {MAX_SUITE_NEST} 层（疑似循环引用）")

    top_gate = str(payload.get("on_failure") or "continue")
    stages = payload.get("stages")
    if stages is None:
        stages = [{"name": "", "cases": payload.get("cases") or [], "on_failure": top_gate}]
    if not isinstance(stages, list) or not stages:
        raise ValueError("套件 stages 必须为非空数组")

    items: list[tuple[TestCase, str, str, str]] = []
    for st in stages:
        if not isinstance(st, dict):
            raise ValueError("stages 的每个元素必须是对象")
        stage_name = str(st.get("name") or "").strip()
        gate = str(st.get("on_failure") or top_gate)
        if gate not in ("continue", "abort"):
            raise ValueError(f"on_failure 只能是 continue / abort，收到 {gate!r}"
                             f"（阶段 {stage_name or '(未命名)'}）")
        cases = st.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError(f"阶段 {stage_name or '(未命名)'} 的 cases 必须为非空数组")
        for entry in cases:
            if isinstance(entry, str):
                path = base_dir / entry
                if not path.is_file():
                    path = Path(entry)
                if not path.is_file():
                    raise ValueError(f"套件条目不存在: {entry}")
                data = json.loads(path.read_text(encoding="utf-8"))
                if is_suite_payload(data):
                    for case, stem, child_stage, child_gate in expand_suite(
                        data, path.parent, depth=depth + 1
                    ):
                        merged = "/".join(x for x in (stage_name, child_stage) if x)
                        items.append((case, stem, merged, child_gate))
                else:
                    case = resolve_dataset_refs(TestCase.model_validate(data), path.parent)
                    items.append((case, path.stem, stage_name, gate))
            else:
                case = resolve_dataset_refs(TestCase.model_validate(entry), base_dir)
                items.append((case, case.case_id, stage_name, gate))
    return items
