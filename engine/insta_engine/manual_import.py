"""手工用例导入：解析 → IR → 批量 AI 转换 → A/B/C 分级草稿。

设计总纲：不做万能解析器——统一中间模型（ManualCase）+ 启发式列映射（规则优先，
失败降级 LLM 建议）+ 产物永远是草稿（AI 红线：人工确认）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .case_audit import audit_case
from .case_quality import (
    clean_locator,
    fmt_issue,
    grade_of,
    quality_issues,
    review,
    rule_code_of,
    sanitize_assertions,
    sanitize_targets,
)
from .dsl import TestCase, dump_dsl, unique_case_id
from .precondition_expand import expand_enabled
from .preconditions import provisionable
from .quota import UsageStore

# -- IR（中间模型） ------------------------------------------------


class ManualStep(BaseModel):
    index: int
    action: str
    data: str | None = None
    expected: str | None = None


class ManualCase(BaseModel):
    title: str
    module: str | None = None
    priority: str | None = None
    tags: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[ManualStep] = Field(default_factory=list)
    expected_result: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)


class TemplateError(ValueError):
    """列映射无法确定（必须项缺失且无模板）。"""


# -- 启发式列映射（规则优先） ---------------------------------------

_KEYWORDS: dict[str, list[str]] = {
    "title": ["用例标题", "标题", "用例名称", "用例名", "用例", "title", "case name", "名称"],
    "module": ["所属模块", "模块", "功能模块", "module", "feature"],
    "priority": ["优先级", "级别", "priority"],
    "preconditions": ["前置条件", "预置条件", "前置", "precondition"],
    "steps_action": ["操作步骤", "测试步骤", "步骤", "操作", "step", "steps", "action"],
    "steps_data": ["测试数据", "输入数据", "数据", "输入", "data", "input"],
    "steps_expected": ["预期结果", "预期", "期望结果", "期望", "预期输出", "expected", "result"],
    "expected_result": ["整体预期", "验收标准", "验证点"],
    "tags": ["标签", "用例类型", "tag", "tags"],
    "external_id": ["用例编号", "编号", "用例id", "case id", "id"],
}
_REQUIRED = ("title", "steps_action")


def _norm(text: str) -> str:
    return re.sub(r"[\s\u3000·_\-—()（）\[\]【】]", "", str(text)).lower()


def _match_column(headers: list[str], keywords: list[str]) -> int | None:
    normed = [_norm(h) for h in headers]
    for keyword in keywords:  # 先精确
        for idx, header in enumerate(normed):
            if header == _norm(keyword):
                return idx
    for keyword in keywords:  # 再子串
        keyword_norm = _norm(keyword)
        for idx, header in enumerate(normed):
            if header and keyword_norm and (keyword_norm in header or header in keyword_norm):
                return idx
    return None


def heuristic_template(headers: list[str]) -> dict:
    """表头 → 列映射模板；必须项缺失抛 TemplateError（提示 --template 或 AI 建议）。"""
    template: dict[str, Any] = {}
    for field, keywords in _KEYWORDS.items():
        idx = _match_column(headers, keywords)
        if idx is not None:
            template[field] = idx
    missing = [f for f in _REQUIRED if f not in template]
    if missing:
        raise TemplateError(
            f"无法从表头识别必须列（缺 {missing}）。"
            "请提供映射模板 --template <json>，或在控制台用 AI 映射建议。"
        )
    return template


def suggest_template_with_ai(client, headers: list[str], sample_rows: list[list[str]]) -> dict:
    """启发式失败时的 LLM 建议映射（prompt 闭源；server 模式由轻云组装）。"""
    from .ai_client import complete_task_raw

    data = complete_task_raw(
        client, "map",
        {"headers": headers, "rows": sample_rows},
        "列映射建议",
    )
    template = {k: v for k, v in data.items() if k in _KEYWORDS}
    missing = [f for f in _REQUIRED if f not in template]
    if missing:
        raise TemplateError(f"AI 映射仍缺必须列 {missing}，请手写映射模板。")
    return template


def resolve_columns(template: dict, headers: list[str]) -> dict:
    """模板值统一为列下标：字符串按表头名解析，整数直用。"""
    resolved: dict[str, Any] = {}
    for field, value in template.items():
        if isinstance(value, int):
            resolved[field] = value
        else:
            names = value if isinstance(value, list) else [value]
            idxs = []
            for name in names:
                matched = _match_column(headers, [str(name)])
                if matched is None:
                    raise TemplateError(f"映射列不存在: {name!r}（表头: {headers}）")
                idxs.append(matched)
            resolved[field] = idxs if isinstance(value, list) else idxs[0]
    return resolved


# -- 解析器 ---------------------------------------------------------------------

_STEP_NUMBER = re.compile(r"^\s*(?:\d+|[①②③④⑤⑥⑦⑧⑨⑩])[.、)）：:]\s*")
_INLINE_NUMBER = re.compile(r"(?:^|(?<=\s))\d{1,3}[.、)）]\s+")  # 行内编号；要求编号后有空格，避免误切"3.5 秒"


def _split_steps(text: str) -> list[str]:
    steps = []
    for line in re.split(r"[\n\r]+", str(text or "")):
        line = line.strip()
        if not line:
            continue
        marks = [m.start() for m in _INLINE_NUMBER.finditer(line)]
        segments = ([line[a:b].strip() for a, b in zip(marks, marks[1:] + [len(line)], strict=True)]
                    if len(marks) >= 2 else [line])
        for segment in segments:
            cleaned = _STEP_NUMBER.sub("", segment).strip()
            if cleaned:
                steps.append(cleaned)
    return steps


def _cell(row: list[str], columns, default=None):
    """columns 可为 int 或 int 列表（多列合并）。"""
    if columns is None:
        return default
    idxs = columns if isinstance(columns, list) else [columns]
    values = [str(row[i]).strip() for i in idxs if i < len(row) and str(row[i]).strip()]
    return " ".join(values) if values else default


def _cases_from_rows(rows: list[list[str]], template: dict, source: dict) -> list[ManualCase]:
    if not rows:
        return []
    headers = [str(h).strip() for h in rows[0]]
    columns = resolve_columns(template, headers)

    cases: list[ManualCase] = []
    for row_no, row in enumerate(rows[1:], start=2):
        title = _cell(row, columns.get("title"))
        if not title:
            continue  # 空行/分隔行
        actions = _split_steps(_cell(row, columns.get("steps_action"), ""))
        datas = _split_steps(_cell(row, columns.get("steps_data"), "")) if columns.get("steps_data") else []
        expecteds = _split_steps(_cell(row, columns.get("steps_expected"), "")) if columns.get("steps_expected") else []
        expected_result = _cell(row, columns.get("expected_result"))
        if len(expecteds) == 1 and len(actions) > 1:
            # 单条预期 + 多步骤 → 按整体预期处理（AI 拆解为末步断言，置信度降级）
            expected_result = expected_result or expecteds[0]
            expecteds = []
        steps = [
            ManualStep(
                index=i + 1,
                action=action,
                data=datas[i] if i < len(datas) else None,
                expected=expecteds[i] if i < len(expecteds) else None,
            )
            for i, action in enumerate(actions)
        ]
        pre = _cell(row, columns.get("preconditions"))
        tags = _cell(row, columns.get("tags"))
        cases.append(ManualCase(
            title=title,
            module=_cell(row, columns.get("module")),
            priority=_cell(row, columns.get("priority")),
            tags=[t for t in re.split(r"[,，;；/|]", tags) if t.strip()] if tags else [],
            preconditions=_split_steps(pre) if pre else [],
            steps=steps,
            expected_result=expected_result,
            source={**source, "row": row_no},
        ))
    return cases


def _read_csv(path: Path) -> list[list[str]]:
    """国内团队的用例表常见 GBK/GB18030 编码：utf-8 失败回退 gb18030。"""
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as fh:
                return [list(row) for row in csv.reader(fh)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise TemplateError(f"CSV 编码无法识别（已尝试 utf-8/gb18030）: {last_error}")


def _read_xlsx(path: Path) -> list[list[str]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise TemplateError("读取 Excel 需要：pip install 'mat-uitest[import]'") from exc
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    return [[("" if cell is None else str(cell)) for cell in row] for row in sheet.iter_rows(values_only=True)]


def _read_markdown(path: Path) -> list[list[str]]:
    """管道表格 → 二维数组。

    ⚠️ **单元格里的 `<br>` 必须还原成换行**（2026-10-11 实测确认）：
    markdown 表格里换行只能写 `<br>`（真换行会把表格切断），所以用例表里的
    「多步」几乎必然写成 `1. 第一步<br>2. 第二步`。而 `_split_steps` 的行内编号正则
    要求编号前是**空白**（`(?<=\\s)`，为的是不误切「3.5 秒」），`<br>` 后是 `>` ⇒ 不切。
    结果：**整列被并成 1 步**，且**不报错**（同族 F14：错误结果看起来完全正常）。
    """
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and not set("".join(cells)) <= set("-: "):  # 跳过分隔行
                rows.append([re.sub(r"<br\s*/?>", "\n", c, flags=re.I) for c in cells])
    return rows


def _read_text(path: Path) -> list[ManualCase]:
    """纯文本粘贴约定：空行分块，`标签: 值`；**多行值写在标签行下面**（缩进不敏感）。

    2026-10-11 修两个静默缺陷（实测最小复现：`操作步骤` 写 2 步、`测试数据`/`预期结果`
    各写 2 行 ⇒ 解析出 **4 步**、`data` 全为 None）：

    ① **非标签行一律追加到 `steps_action`** —— 不管上一个标签是什么。于是
       `测试数据:` 的第二行、`预期结果:` 的第二行都被当成**额外的步骤**（多出 2 步），
       而它们本该属于各自的字段。现在改为**追加到「最近一次出现的那个标签」**。
    ② **`steps_data` 从未被读取** —— `ManualStep.data` 永远是 `None`，
       用户写的测试数据**静默丢失**。现在按行配对填入。

    ⚠️ 这两条都**不报错**：解析照常返回用例，只是内容错了（多步骤 / 数据丢失）——
    与台账 F14「代理判据冒充证据」同族。向后兼容：无标签的行若紧跟 `操作步骤`，
    行为与修复前一致（仍进 `steps_action`）。
    """
    keyword_of = {kw: field for field, kws in _KEYWORDS.items() for kw in kws}
    known = {_norm(k) for k in keyword_of}
    cases: list[ManualCase] = []
    block: list[str] = []
    blocks = [block]
    for line in path.read_text(encoding="utf-8").splitlines() + [""]:
        if line.strip():
            block.append(line)
        elif block:
            block = []
            blocks.append(block)
    row_no = 1
    for current in blocks:
        if not current:
            continue
        fields: dict[str, str] = {}
        last_field: str | None = None          # ← 最近一次出现的标签
        for line in current:
            match = re.match(r"^([^:：]{1,12})[:：]\s*(.*)$", line.strip())
            if match and _norm(match.group(1)) in known:
                last_field = keyword_of[_norm(match.group(1))]
                fields[last_field] = (fields.get(last_field, "") + "\n"
                                      + match.group(2)).strip()
            elif last_field:                    # 续行 → 归给**最近那个标签**
                fields[last_field] = (fields.get(last_field, "") + "\n"
                                      + line.strip()).strip()
        if not fields.get("title"):
            continue
        row_no += 1
        actions = _split_steps(fields.get("steps_action", ""))
        datas = _split_steps(fields.get("steps_data", ""))
        expecteds = _split_steps(fields.get("steps_expected", ""))
        expected_result = (fields.get("expected_result") or "").strip() or None
        if len(expecteds) == 1 and len(actions) > 1:
            # 与表格路径同规则：单条预期 + 多步骤 ⇒ 按整体预期处理
            expected_result = expected_result or expecteds[0]
            expecteds = []
        cases.append(ManualCase(
            title=fields["title"].strip(),
            module=(fields.get("module") or "").strip() or None,
            priority=(fields.get("priority") or "").strip() or None,
            tags=[t for t in re.split(r"[,，;；/|]", fields.get("tags", "")) if t.strip()],
            preconditions=_split_steps(fields.get("preconditions", "")),
            steps=[ManualStep(index=i + 1, action=a,
                              data=datas[i] if i < len(datas) else None,
                              expected=expecteds[i] if i < len(expecteds) else None)
                   for i, a in enumerate(actions)],
            expected_result=expected_result,
            source={"format": "text", "file": path.name, "row": row_no},
        ))
    return cases



#: XMind 里「前置条件」子主题的识别词（与 `_KEYWORDS["preconditions"]` 同源）。
#: 为什么必须有：XMind 路径此前**一条前置条件都提不出来**（实测 `preconditions: []`），
#: 而 xlsx 路径能提 ⇒ **同一批用例换个源格式，质量差一档**，且丢掉的那一列正是
#: 7 条假"定位失败"的根因（见 preconditions.py 的模块说明）。
_PRECONDITION_MARKERS = ("前置条件", "预置条件", "前置", "precondition")


def _is_precondition_topic(title: str) -> bool:
    """`前置条件：已进入首页` / `前置条件` 都算；只看分隔符**前**那段，避免误判步骤文案。"""
    head = re.split(r"[：:]", title, maxsplit=1)[0].strip().lower()
    return any(marker in head for marker in _PRECONDITION_MARKERS)


def _split_precondition(title: str) -> str:
    """`前置条件：已进入首页` → `已进入首页`；没有分隔符则返回空串（条目由子主题承载）。"""
    for sep in ("：", ":"):
        if sep in title:
            return title.split(sep, 1)[1].strip()
    return ""


def _xmind_tree_to_cases(tree: dict, module: str, source: dict) -> list[ManualCase]:
    """约定：root=画布主题 → 二级=用例标题 → 三级=步骤（可带四级子主题=预期）。

    三级里若有「前置条件」主题，则它（连同其子主题）算**前置条件**，不算步骤 ——
    与表格路径的 `preconditions` 列同一语义。
    """
    cases = []
    for case_topic in tree.get("children", []):
        title = str(case_topic.get("title", "")).strip()
        steps: list[ManualStep] = []
        preconditions: list[str] = []
        for step_topic in case_topic.get("children", []):
            action = str(step_topic.get("title", "")).strip()
            if not action:
                continue
            if _is_precondition_topic(action):
                inline = _split_precondition(action)
                if inline:
                    preconditions.append(inline)
                preconditions.extend(
                    str(sub.get("title", "")).strip()
                    for sub in step_topic.get("children", [])
                    if str(sub.get("title", "")).strip())
                continue
            expected = None
            subs = step_topic.get("children", [])
            if subs:
                expected = str(subs[0].get("title", "")).strip() or None
            steps.append(ManualStep(index=len(steps) + 1, action=action, expected=expected))
        if title and steps:
            cases.append(ManualCase(title=title, module=module, preconditions=preconditions,
                                    steps=steps, source={**source, "title": title}))
    return cases


def _read_xmind(path: Path) -> list[ManualCase]:
    """XMind（Zen content.json / legacy content.xml）→ IR（后续版本）。"""
    import xml.etree.ElementTree as ET
    import zipfile

    cases: list[ManualCase] = []
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise TemplateError(f"XMind 文件无法读取（损坏或非 XMind 格式）: {exc}") from exc
    with zf:
        names = zf.namelist()
        if "content.json" in names:
            sheets = json.loads(zf.read("content.json"))
            for si, sheet in enumerate(sheets, 1):
                module = str(sheet.get("title", "")).strip() or f"Sheet{si}"

                def walk(topic: dict) -> dict:
                    return {"title": str(topic.get("title", "")),
                            "children": [walk(t) for t in (topic.get("children") or {}).get("attached", [])]}

                root = walk(sheet.get("rootTopic") or {})
                # 约定：画布主题(root) 的二级子主题 = 用例标题（后续版本）
                cases.extend(_xmind_tree_to_cases(root, module,
                             {"format": "xmind", "file": path.name, "sheet": si}))
        elif "content.xml" in names:
            tree = ET.fromstring(zf.read("content.xml"))

            def xml_walk(topic) -> dict:
                return {"title": topic.findtext("title", "").strip(),
                        "children": [xml_walk(t) for t in topic.find("children/topics") or []]}

            sheets = [tree] if tree.tag == "sheet" else tree.findall(".//sheet")
            for si, sheet in enumerate(sheets, 1):
                root_topic = sheet.find("topic")
                if root_topic is None:
                    continue
                root = xml_walk(root_topic)
                module = root["title"] or f"Sheet{si}"
                cases.extend(_xmind_tree_to_cases(root, module,
                             {"format": "xmind", "file": path.name, "sheet": si}))
        else:
            raise TemplateError("XMind 缺少 content.json/content.xml")
    return cases


def parse_file(path: Path, template: dict | None = None) -> tuple[list[ManualCase], dict]:
    """解析文件 → (IR 列表, 实际使用的映射模板)。表格类无模板时走启发式。"""
    path = Path(path)
    suffix = path.suffix.lower()
    source_base = {"format": suffix.lstrip("."), "file": path.name, "template": "custom" if template else "heuristic"}
    if suffix == ".txt":
        return _read_text(path), {"mode": "text"}
    if suffix == ".xmind":
        return _read_xmind(path), {"mode": "xmind"}
    readers = {".csv": _read_csv, ".xlsx": _read_xlsx, ".xlsm": _read_xlsx, ".md": _read_markdown, ".markdown": _read_markdown}
    if suffix not in readers:
        raise TemplateError(f"不支持的格式 {suffix}（支持 {'/'.join(IMPORT_SUFFIXES)}）")
    rows = readers[suffix](path)
    if len(rows) < 2:
        raise TemplateError("文件内容不足（至少需要表头 + 1 行数据）")
    used_template = template or heuristic_template(rows[0])
    return _cases_from_rows(rows, used_template, source_base), used_template


# -- AI 批量转换与分级 ---------------------------------------------

GRADES = ("A", "B", "C")

IMPORT_SUFFIXES = (".csv", ".xlsx", ".xlsm", ".md", ".markdown", ".txt", ".xmind")

# -- 用例质量闸门：**单一实现**，与生成路径（`mat gen`）共用 ---------------------
#
# ⚠️ 这里曾经是这套判据的**唯一实现**，而 `mat gen` 的产物**一道闸都没有**
#    （LLM 产物校验完 schema 直接落盘）。同一件事两个口径，必然漂移。
#    实现已搬进 `case_quality.py`，两条路径都走它；
#    下面这些私有名保留为**别名**，既有的调用点与测试不必动。
_clean_locator = clean_locator
_sanitize_targets = sanitize_targets
_sanitize_assertions = sanitize_assertions
_quality_issues = quality_issues


def _insert_after_launch(raw_case: dict, steps: list[dict]) -> None:
    """把展开出的导航步骤插到 `launch` **之后**（没有 launch 就插在最前）。

    为什么要在 launch 之后：展开的语义是"从**冷启动首页**走到某个状态"，
    所以起点必须是 launch 建立的那个首页，而不是设备当时停的地方。

    ⚠️ `id` 在**这里**分配（不是在规整时）：唯一性只有看到**整条用例**才知道，
    而重复 step id 会让重试时截图/证据互相覆盖（`dsl.Step` 也要求 id 存在）。
    """
    target_steps = raw_case.setdefault("steps", [])
    used = {str(s.get("id") or "") for s in target_steps if isinstance(s, dict)}
    for step in steps:
        sid = str(step.get("id") or "")
        if not sid or sid in used:
            sid = next(f"pre{i}" for i in range(1, len(used) + len(steps) + 2)
                       if f"pre{i}" not in used)
            step["id"] = sid
        used.add(sid)
    idx = 1 if target_steps and str(target_steps[0].get("action") or "") == "launch" else 0
    target_steps[idx:idx] = steps


def _expand_preconditions(client, raw_case: dict, items: tuple[str, ...], package: str,
                          controls: str, graph: str, record: dict) -> list[str]:
    """R9b：把「可置备的页面状态」展开成导航步骤并插入（返回要记录的 issues）。

    ⚠️ 调用点在 `review()` 的**第 ② 步**（`ensure_launch` 之后、消毒/检查之前）——
    这样展开出来的步骤**也会过闸门**。详见 `case_quality.review` 的顺序说明。
    """
    from .precondition_expand import MARK, expand

    steps, notes, called = expand(client, list(items), package, controls, graph)
    if called:
        record["expand_calls"] = 1  # 额度记账：复用 gen 任务，0.5 次/条（没发出去就不记）
    pre = "、".join(f"「{i}」" for i in items)
    # 展开器自己的说明（失败原因、丢掉的动作）都源于**模型输出** ⇒ 挂 `[LLM]`；
    # "已展开/未能展开"这条是**规则 R9b 的结论** ⇒ 挂 `[R9b]`。
    tagged = [fmt_issue("LLM", str(n)) for n in notes]
    if not steps:
        return [fmt_issue("R9b", f"前置条件 {pre} **未能展开**（模型判定无法确定路径）："
                                 "执行前请手工把设备置备到该状态"), *tagged]
    _insert_after_launch(raw_case, steps)
    record["expanded"] = list(items)
    return [fmt_issue("R9b", f"前置条件 {pre} 已展开为 {len(steps)} 步导航"
                             f"（intent 前缀 `{MARK}`），**请人工确认路径** —— "
                             "模型给的路径可能不对，而错路径比不展开更难查"), *tagged]


def convert_case(client, case: ManualCase, package: str, controls: str = "",
                 graph: str = "", index_dir: Path | None = None) -> dict:
    """单条 IR → DSL 草稿 + 分级。永不抛出（整批容错），失败降为 C。"""
    record: dict[str, Any] = {
        "title": case.title, "grade": "C", "confidence": 0.0, "issues": [],
        "case_file": None, "ir": case.model_dump(),
    }
    try:
        from .ai_client import complete_task_raw

        app_info: dict = {"package": package}
        if controls:
            app_info["controls"] = controls
        if graph:
            app_info["graph"] = graph  # 页面结构上下文（默认关闭）
        data = complete_task_raw(
            client, "convert",
            {"ir": case.model_dump(), "app_info": app_info},
            f"导入转换:{case.title}",
        )
        if data.get("case") is None:
            # 模型按约定优雅拒绝（C 级不硬造）
            reported = [fmt_issue("LLM", str(i)) for i in data.get("issues", [])]
            return {**record, "grade": "C", "confidence": float(data.get("confidence", 0.2)),
                    "issues": reported or [fmt_issue("X0", "模型判定无法转换为 UI 自动化用例")]}
        confidence = float(data.get("confidence", 0.5))
        # 质量闸门：**一站式**（保证起始状态 → 展开前置条件 → 消毒 → 检查）。
        # ⚠️ 必须走 `review()` 而不是自己挑几个子函数调 —— 本处曾经只挑三个子函数，
        # 于是漏掉了 `ensure_launch` 与"前置条件需置备"两条判据（见 review 的说明）。
        hook = None
        page_states = provisionable(case.preconditions)
        if page_states and expand_enabled():
            def hook(raw_case, _items=tuple(page_states)):  # noqa: B023 - 默认参数绑定，非晚绑定
                return _expand_preconditions(client, raw_case, _items, package,
                                             controls, graph, record)
        issues = review(data["case"], case.preconditions, expand=hook)
        # 模型自己报的问题：挂 `[LLM]`（**没有对应规则编号**这件事本身要可见 ——
        # R16 要求每条 issue 都能指回一个编号，模型的自述是唯一的例外）
        issues += [fmt_issue("LLM", str(i)) for i in data.get("issues", [])]
        draft = TestCase.model_validate(data["case"])
        # 前置条件**原样带进 DSL** —— 此前在 DSL 边界被丢掉（见 preconditions 模块说明）
        draft.preconditions = [str(p) for p in (case.preconditions or [])]
        draft.meta = {
            "module": case.module, "priority": case.priority, "tags": case.tags,
            "source": {**case.source, "title": case.title},
        }
        if record.get("expanded"):
            # 留档：这条用例的导航步骤是**模型展开**的（人工复核时的抓手）
            draft.meta["expanded_preconditions"] = list(record["expanded"])
        for i, step in enumerate(draft.steps, 1):
            step.source_step = i
        # L1/L6 定位审计（来自 feature/console-v2 的 `case_audit`）：
        # ⚠️ **只保留与 `case_quality` 不重叠的两条** —— L2–L5（无障碍后缀 / 动态内容 /
        # 断言自指 / 首步可达）已由 R5 / R4 / R2 / R6 覆盖，不再重复实现，否则同一个
        # 缺陷会被两套各报一次（见 `case_audit` 的模块说明）。放在这里是因为它要吃
        # **消毒后**的 draft；它返回的警告已带 `[R18]`/`[R19]` 编号，直接参与分级。
        audit_warns = audit_case(draft, index_dir=index_dir)
        if audit_warns:
            issues += list(audit_warns)
        if draft.meta.get("priority") is None:
            draft.meta.pop("priority")
        if draft.meta.get("module") is None:
            draft.meta.pop("module")
        if not any(step.assertions for step in draft.steps):
            issues.append(fmt_issue("R12", "草稿无断言：请依据手工用例预期结果补充断言"))
        # ⚠️ 模型自评**不参与分级**（2026-10-13）：它是**不可验证的模型判断**，
        #    拿它当质量信号就是代理指标（同族 D10/F14 —— "看起来像个分数"）。
        #    它只作为**提示**（R14），并在 record 里单独输出供人参考。
        if confidence < 0.5:
            issues.append(fmt_issue(
                "R14", f"模型自评低（{confidence:.2f}）：它自己都不确定，请重点核对步骤与定位"
                       "（**自评不参与分级**，只是提示）"))
        grade = grade_of(issues)
        record.update(grade=grade, confidence=confidence, issues=issues)
        # ⚠️ **只要产出了 DSL 就落草稿**（不再看等级）：草稿本来就是给人改的。
        #    因为一条断言有问题就不给草稿，等于把已经花掉的转换额度扔掉，
        #    还把"改哪一条"的活推回给手工用例 —— 方向反了。
        #    C 级仍然响亮标出（它是"必失败，必须改"），但**不再等于"什么都不给"**。
        import uuid
        slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", case.title).strip("-")[:24] or "case"
        record["case_file"] = f"import-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}-{slug}.json"
        record["dsl"] = json.loads(dump_dsl(draft))
        return record
    except Exception as exc:
        record["issues"] = [*(record.get("issues") or []),
                            fmt_issue("X0", f"转换失败: {type(exc).__name__}: {exc}")]
        record["grade"] = "C"
        record["confidence"] = 0.0
        return record


def quality_stats(results: list[dict]) -> dict:
    """批次质量口径（R15）。**名字必须诚实** —— 这里算的是：

      · `no_blocking_rate`：**没有阻断缺陷**（无 C 级、且产出了草稿）的比例
      · `clean_rate`：A 级（零 warn/block）比例
      · `by_rule`：按**规则编号**统计的命中次数（R16 —— 让"哪一类最常犯"可回答）

    ⚠️ **这两个都不是「可执行率」**。真「可执行率」＝"不改就能跑绿的比例"，
    必须与**真机跑测数据** join 才算得出来（跑过才知道）。拿离线口径冒充它
    就是代理指标（同族 D10/F10：一个看起来像分数的数字会被当成证据引用）。
    """
    total = len(results)
    blocked = sum(1 for r in results if r["grade"] == "C" or not r.get("dsl"))
    by_rule: dict[str, int] = {}
    for record in results:
        for issue in record.get("issues") or []:
            code = rule_code_of(str(issue))
            by_rule[code] = by_rule.get(code, 0) + 1
    return {
        "total": total,
        "blocked": blocked,
        "no_blocking_rate": round((total - blocked) / total, 3) if total else 0.0,
        "clean_rate": round(sum(1 for r in results if r["grade"] == "A") / total, 3) if total else 0.0,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def convert_batch(client, cases: list[ManualCase], package: str, usage: UsageStore,
                  on_progress: Callable[[int, int], None] | None = None, controls: str = "",
                  graph: str = "", cancel_event=None,
                  index_dir: Path | None = None) -> dict:
    """批量转换：容量预检 → 逐条转换 → 汇总报告（含原文副本，支持重转）。

    额度口径：转换 0.5/条；**带「可置备页面状态」前置条件的用例多 0.5**（R9b 的展开
    复用 `gen` 任务，一次调用）。预检按"最多会用多少"算 —— **宁可多留，不可中途爆**。

    `cancel_event` 置位后在**下一条开始前**退出（协作式取消，与 `Runner` 同语义：
    当前条的 AI 调用无法强杀，已完成条目照常入账落盘）⇒ `summary.total` 是
    **已完成**条数，中止时小于 `cases` 数（UI 文案由调用方区分）。
    """
    expanding = expand_enabled()
    extra = sum(1 for c in cases if expanding and provisionable(c.preconditions))
    amount = round((len(cases) + extra) * 0.5, 2)
    usage.ensure_capacity(amount)  # 不足直接 QuotaExceeded，不产生半批结果
    results = []
    for idx, case in enumerate(cases, 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        record = convert_case(client, case, package, controls=controls, graph=graph,
                              index_dir=index_dir)
        results.append(record)
        # 按**实际**调用次数核销（展开可能被跳过，预检只是保守上界）
        usage.consume(round(0.5 * (1 + record.get("expand_calls", 0)), 2))
        if on_progress:
            on_progress(idx, len(cases))
    summary = {g: sum(1 for r in results if r["grade"] == g) for g in GRADES}
    # `quality` 单独一层：`summary` 的键被既有调用方/测试依赖，别往里塞新键
    return {"results": results, "summary": {"total": len(results), **summary},
            "quality": quality_stats(results)}


def save_import_bundle(root: Path, source_path: Path, package: str, batch: dict,
                       used_template: dict) -> tuple[str, Path]:
    """落盘导入批次：artifacts/imports/<id>/import.json + 原文副本 + A/B 草稿写 cases/。"""
    import_id = datetime.now().strftime("%H%M%S") + "-" + hashlib.sha1(
        source_path.read_bytes()).hexdigest()[:8]
    bundle_dir = root / "artifacts" / "imports" / import_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = root / "cases"
    # 只建一次：此前写在循环里，第二条草稿会对已存在目录再 mkdir(exist_ok=True)，
    # 在严格环境下直接 EEXIST 失败（本机沙箱就是），且语义上本就是重复动作。
    cases_dir.mkdir(parents=True, exist_ok=True)
    used_ids = {r["case_file"] for r in batch["results"] if r.get("case_file")}
    for record in batch["results"]:
        if record.get("dsl"):
            data = dict(record["dsl"])  # 不修改调用方数据
            data["caseId"] = unique_case_id(str(data.get("caseId", "case_import")), cases_dir)
            case_path = cases_dir / record["case_file"]
            case_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            used_ids.add(record["case_file"])
    payload = {
        "import_id": import_id, "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_file": source_path.name, "package": package, "template": used_template, **batch,
    }
    (bundle_dir / "import.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        (bundle_dir / f"source{source_path.suffix}").write_bytes(source_path.read_bytes())
    except OSError:
        pass
    return import_id, bundle_dir
