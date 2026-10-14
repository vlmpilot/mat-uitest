"""/api/case GET/POST 与 /api/import/preview|convert（用例与导入域）。"""

from __future__ import annotations

import base64
import json
import tempfile
import uuid
from pathlib import Path

from ..dsl import TestCase
from ..quota import UsageStore
from .runtime import _require_fresh_code, operator_of, start_job


def _err(message: str) -> tuple[int, bytes, str]:
    return 400, json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"), \
        "application/json; charset=utf-8"


def api_case_get(root: Path, name: str) -> tuple[int, bytes, str]:
    """GET /api/case?name=：返回用例原文件（JSON 字节；非法名/不存在由调用方语义给出）。"""
    if "/" in name or ".." in name:
        return _err("非法用例名")
    target = root / "cases" / name
    if not target.exists() or not target.is_relative_to((root / "cases").resolve()):
        return 404, b'{"error": "not found"}', "application/json; charset=utf-8"
    return 200, target.read_bytes(), "application/json; charset=utf-8"


def api_case_save(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/case：落盘前 DSL 校验（P6：两侧同 schema）+ 启发式警告（不阻断）。"""
    name = str(body.get("name", "")).strip()
    raw = body.get("content", "")
    if isinstance(raw, (dict, list)):
        # 边界兜底：结构化编辑器曾把对象直接发上来——str(dict) 是 Python 单引号串，
        # model_validate_json 必炸（key must be a string at line 1 column 2）
        content = json.dumps(raw, ensure_ascii=False)
    else:
        content = str(raw)
    if not name.endswith(".json") or "/" in name or ".." in name:
        return 400, {"error": "非法用例名"}
    try:
        case = TestCase.model_validate_json(content)  # 落盘前校验（P6：两侧同 schema）
    except Exception as exc:
        return 400, {"error": f"用例 JSON 不合法: {exc}"[:300]}
    (root / "cases").mkdir(exist_ok=True)
    (root / "cases" / name).write_text(content, encoding="utf-8")
    return 200, {"ok": True, "warnings": _case_warnings(root, case)}


def _case_warnings(root: Path, case: TestCase) -> list[str]:
    """落盘后的启发式提醒（发起人 2026-10-12 P0 保存校验升级；**只警告不阻断**）。

    两类来源：① 结构检查——action 与 params/target 不匹配（runner 会直接抛错
    或空转）、断言缺 target/expected（执行侧硬失败）；② L1–L6 定位审计
    （`case_audit.audit_case`，与转换/导入侧同一实现：text/res-id 清单校验、
    无障碍注解、动态内容、断言自指、首步可达性、一次性弹窗依赖）。
    """
    from ..case_audit import audit_case

    warns: list[str] = []
    pkg = (case.target or {}).get("package", "")
    for st in case.steps:
        label = f"步骤 {st.id}（{st.action.value}）"
        if st.action.value == "input" and not str(st.params.get("text", "")).strip():
            warns.append(f"{label}：input 缺少输入文本（params.text）")
        if st.action.value == "launch" and not (st.params.get("package") or pkg):
            warns.append(f"{label}：launch 未指定包名（params.package 与 target.package 均为空）")
        if st.action.value in ("tap", "long-press", "clear") and st.target is None:
            warns.append(f"{label}：缺少定位目标（target）")
        for i, a in enumerate(st.assertions, 1):
            alabel = f"步骤 {st.id} 断言{i}（{a.type.value}）"
            if a.target is None:
                warns.append(f"{alabel}：缺少 target（执行时会直接失败）")
            if a.type.value in ("text-equals", "text-contains") and not (a.expected or "").strip():
                warns.append(f"{alabel}：缺少 expected 期望值")
    warns.extend(audit_case(case, root / "artifacts" / "page_index"))
    return warns


def api_controls(root: Path, package: str) -> tuple[int, dict]:
    """GET /api/controls?package=：page_index 控件清单（编辑器 target 下拉候选）。

    无索引返回空列表（前端降级为手填 + 提示），不报错——索引缺失是常态而非异常。"""
    from ..page_index import controls_list_for

    pkg = package.strip()
    if not pkg or "/" in pkg or ".." in pkg:
        return 400, {"error": "需要合法的 package"}
    items = controls_list_for(pkg, root / "artifacts" / "page_index")
    return 200, {"package": pkg, "count": len(items), "controls": items}


def _decode_upload(body: dict) -> tuple[str, Path]:
    """校验并落盘上传文件（≤5MB），返回 (文件名, 临时路径)。"""
    from ..manual_import import IMPORT_SUFFIXES

    name = str(body.get("name", "")).strip()
    suffix = Path(name).suffix.lower()
    if suffix not in IMPORT_SUFFIXES:
        raise ValueError(f"不支持的格式 {suffix or '(无后缀)'}（支持 {'/'.join(IMPORT_SUFFIXES)}）")
    raw = base64.b64decode(str(body.get("content_b64", "")) or b"", validate=True)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("文件超过 5MB 上限")
    tmp = Path(tempfile.mkstemp(suffix=suffix)[1])
    tmp.write_bytes(raw)
    return name, tmp


def api_import_preview(root: Path, body: dict) -> tuple[int, dict]:
    from ..manual_import import TemplateError, parse_file

    try:
        name, tmp = _decode_upload(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    template = body.get("template") if isinstance(body.get("template"), dict) else None
    try:
        cases, used = parse_file(tmp, template)
    except (TemplateError, ValueError) as exc:
        return 400, {"error": str(exc)}
    finally:
        tmp.unlink(missing_ok=True)
    return 200, {
        "count": len(cases), "template": used,
        # used 是映射 dict，前端直接渲染会出 [object Object]——渲染层给出可读文案
        "template_label": _template_label(used),
        "titles": [c.title for c in cases[:20]],
        "sample": [c.model_dump() for c in cases[:2]],
    }


# parse_file 返回的映射模板字段 → 中文（渲染层文案，与 _KEYWORDS 字段集对应）
_TEMPLATE_FIELD_LABELS = {
    "title": "标题", "module": "模块", "priority": "优先级", "preconditions": "前置",
    "steps_action": "操作步骤", "steps_data": "测试数据", "steps_expected": "步骤预期",
    "expected_result": "整体预期", "tags": "标签", "external_id": "编号",
}


def _template_label(used: dict) -> str:
    """把 parse_file 的返回模板渲染成人类可读文案（发起人 2026-10-12 反馈 [object Object]）。

    used 三种形态：{"mode":"text"/"xmind"}（TXT/XMind 无列映射）；{字段: 列索引}
    （表头启发式或用户上传的自定义映射）。"""
    mode = used.get("mode")
    if mode == "text":
        return "TXT 分段"
    if mode == "xmind":
        return "XMind"
    mapping = {k: v for k, v in used.items() if isinstance(v, int)}
    if not mapping:
        return "自定义模板"
    parts = [f"{_TEMPLATE_FIELD_LABELS.get(k, k)}→第{v + 1}列" for k, v in mapping.items()]
    return "表头映射 " + "、".join(parts[:4]) + ("…" if len(parts) > 4 else "")


def api_import_convert(root: Path, body: dict) -> tuple[int, dict]:
    from ..ai_client import GenClient
    from ..manual_import import TemplateError, convert_batch, parse_file, save_import_bundle
    from ..page_index import controls_context_for, graph_context_for

    package = str(body.get("package", "")).strip()
    if not package:
        return 400, {"error": "需要 package"}
    try:
        name, tmp = _decode_upload(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    template = body.get("template") if isinstance(body.get("template"), dict) else None
    upload = root / "artifacts" / "imports" / f"upload-{uuid.uuid4().hex[:6]}{tmp.suffix}"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(tmp.read_bytes())
    tmp.unlink(missing_ok=True)
    base_dir = root / "artifacts" / "page_index"
    # 与 CLI 的 convert 保持一致：都注入控件清单（防幻觉底座）+ 页面结构（默认关）
    controls = controls_context_for(package, base_dir)
    graph = graph_context_for(package, base_dir)

    def job(emit, cancel):
        _require_fresh_code()
        try:
            cases, used = parse_file(upload, template)
        except (TemplateError, ValueError) as exc:
            raise RuntimeError(f"解析失败: {exc}") from exc
        if not cases:
            raise RuntimeError("未解析到用例")
        # cancel 接入 convert_batch：中止在「下一条开始前」生效（当前条 AI
        # 调用无法强杀），已完成条目照常落盘；此前 cancel 在本任务中无人检查——
        # 点中止要等整批转完才退出（发起人 2026-10-12 反馈「中止的比较慢」）。
        # 逐条进度事件不再发（发起人 2026-10-12：转换不需要每条用例的进展提示），
        # 结果由任务 summary 汇报。
        batch = convert_batch(GenClient.from_env(), cases, package, UsageStore(),
                              controls=controls, graph=graph, cancel_event=cancel,
                              index_dir=base_dir)
        _, bundle = save_import_bundle(root, upload, package, batch, used)
        summary = batch["summary"]
        scope = (f"已中止：完成 {summary['total']}/{len(cases)} 条" if cancel.is_set()
                 else f"共 {summary['total']} 条")
        return (f"{scope} → A {summary['A']} / B {summary['B']} / C {summary['C']}"
                f"（草稿在 cases/）"), None

    return 200, {"job_id": start_job("import", job, meta={"operator": operator_of(body)})}


# -- 套件（批量运行）管理：cases/*.suite.json ---------------------------------------------
# 命名（发起人 2026-10-11 定稿）：资产名=「套件」（.suite.json 文件），动作按钮=「批量运行」。
# 不引入「测试计划」概念——定时/CI/多设备矩阵属调度层，等真实需求再挂在套件之上。

def _suite_file(root: Path, name: str) -> Path | None:
    stem = name.removesuffix(".suite.json").removesuffix(".json")
    if not stem or "/" in stem or ".." in stem:
        return None
    return root / "cases" / f"{stem}.suite.json"


def list_suites(root: Path) -> list[dict]:
    suites = []
    cases_dir = root / "cases"
    if cases_dir.exists():
        for f in sorted(cases_dir.glob("*.suite.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                suites.append({"file": f.name, "title": data.get("name", ""),
                               "count": len(data.get("cases", []))})
            except (OSError, ValueError):
                continue
    return suites


def api_suites_list(root: Path) -> tuple[int, dict]:
    return 200, {"suites": list_suites(root)}


def api_suite_get(root: Path, name: str) -> tuple[int, dict]:
    f = _suite_file(root, name)
    if f is None or not f.exists():
        return 404, {"error": f"套件不存在: {name}"}
    data = json.loads(f.read_text(encoding="utf-8"))
    return 200, {"file": f.name, "title": data.get("name", ""), "cases": data.get("cases", [])}


def api_suite_save(root: Path, body: dict) -> tuple[int, dict]:
    title = str(body.get("title", "")).strip()
    cases = body.get("cases")
    if not title:
        return 400, {"error": "需要套件标题"}
    if not isinstance(cases, list) or not cases:
        return 400, {"error": "cases 必须是非空的用例文件名数组"}
    missing = [c for c in cases if not isinstance(c, str) or not (root / "cases" / Path(c).name).exists()]
    if missing:
        return 400, {"error": f"以下用例不存在: {'、'.join(missing[:10])}", "missing": missing}
    import re as _re
    stem = _re.sub(r'[\\/:*?"<>|]', "", title).strip() or "suite"
    out = root / "cases" / f"{stem}.suite.json"
    # 编辑语义（发起人 2026-10-12 反馈「编辑套件保存，会创建一个新的套件」）：body 带
    # file（原文件名）即为编辑——标题没改就原地覆盖；改了标题=重命名（写新删旧），
    # 且不得撞名覆盖别的套件。不传 file 视为新建，按标题落盘（同名覆盖保持旧序）。
    orig = _suite_file(root, str(body.get("file", "")))
    if orig is not None:
        if not orig.exists():
            return 404, {"error": f"原套件不存在: {body.get('file')}（可能已被删除，请关闭后重开）"}
        if out != orig and out.exists():
            return 400, {"error": f"已存在同名套件 {out.name}，请换个标题"}
    out.write_text(json.dumps({"name": title, "cases": [Path(c).name for c in cases]},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if orig is not None and out != orig:
        orig.unlink()
    return 200, {"ok": True, "file": out.name}


def api_suite_delete(root: Path, body: dict) -> tuple[int, dict]:
    f = _suite_file(root, str(body.get("file", "")))
    if f is None or not f.exists():
        return 404, {"error": "套件不存在"}
    f.unlink()
    return 200, {"ok": True}


def api_case_delete(root: Path, body: dict) -> tuple[int, dict]:
    """删除用例文件（发起人 2026-10-12 三问之一：旧批草稿只能进文件系统清理）。

    两层防护：① 只接受 cases/ 下普通用例（.suite.json/路径穿越一律拒绝）；
    ② **被套件引用的用例不可删**（409 + 引用清单）——套件条目兼容「cases/x.json」
    前缀形态，按文件名匹配。运行产物在 artifacts/<目录名> 独立存放，删用例
    不影响历史报告可追溯。
    """
    name = str(body.get("name", "")).strip()
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return 400, {"error": f"非法用例名: {name}"}
    f = root / "cases" / Path(name).name
    if name.endswith(".suite.json") or not name.endswith(".json") or not f.exists():
        return 400, {"error": f"用例不存在: {name}"}
    refs: list[str] = []
    for sf in sorted((root / "cases").glob("*.suite.json")):
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = data.get("cases", [])
        if any(isinstance(c, str) and Path(c).name == name for c in entries):
            refs.append(sf.name)
    if refs:
        return 409, {"error": f"被 {len(refs)} 个套件引用，先从套件移除再删除：{'、'.join(refs[:5])}",
                     "refs": refs}
    f.unlink()
    return 200, {"ok": True, "deleted": name}
