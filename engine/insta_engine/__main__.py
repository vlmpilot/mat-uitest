"""MAT 命令行入口。

正式 CLI（insta，Go 单二进制）在 cli/ 目录；体验版用法：
  mat init                                   # 工作区脚手架
  mat devices                                # 列出在线设备
  mat gen "描述" --package com.xxx [-o case.json] [--calibrate]
  mat run cases/case.json|suite.json [--serial ...] [--artifacts ...]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, suite_cmd
from .ai_client import GenClient
from .credentials import CredentialStore
from .device import list_adb_devices
from .doctor import VISION_OPT_IN_HINT
from .dsl import TestCase, dump_dsl
from .heals import apply_heals, collect_proposals
from .quota import UsageStore
from .report import render_html, render_index_html, render_junit_xml
from .runner import Runner
from .skill import KNOWN_TARGETS
from .suite import load_case_or_suite

_EXAMPLE_CASE = {
    "caseId": "case_example",
    "name": "设置冒烟",
    "target": {"package": "com.android.settings"},
    "steps": [
        {"id": "s1", "intent": "启动设置", "action": "launch", "params": {"package": "com.android.settings"}},
        {"id": "s2", "intent": "回到桌面", "action": "home"},
        {"id": "s3", "intent": "再次启动设置", "action": "launch", "params": {"package": "com.android.settings"}},
    ],
}


def cmd_init(_args) -> int:
    for directory in ("cases", "artifacts"):
        Path(directory).mkdir(exist_ok=True)
    example = Path("cases/example-settings.json")
    if not example.exists():
        example.write_text(json.dumps(_EXAMPLE_CASE, ensure_ascii=False, indent=2), encoding="utf-8")
    from . import promptstore
    seeded = promptstore.seed()  # 播种 prompts/*.prompt（wheel 安装后 AI 能力开箱即用的前提）
    print(f"工作区就绪: {example.resolve()}")
    if seeded:
        print(f"prompt 模板已播种: {', '.join(seeded)}")
        print("  （AI 生成 / 导入转换 / 视觉兜底开箱即用；按需自行调优）")
    else:
        print("prompt 模板: 已存在，跳过播种")
    print("下一步:")
    print("  mat devices")
    print("  mat run cases/example-settings.json")
    return 0


def _connect_driver(serial: str | None, platform: str = "android"):
    """连接设备；失败时给出可行动的排查提示（而非原始 traceback）。

    平台路由已收敛到 `driver_factory.connect_driver` 单源（评审 I4：此前 CLI 与
    web_server 各有一份路由，必然漂移）；本函数只负责渲染可行动的排查提示。
    """
    from .driver_factory import connect_driver

    try:
        return connect_driver(serial, platform)
    except Exception as exc:
        if platform == "ios":
            print(f"连接 iOS 设备失败: {type(exc).__name__}: {exc}")
            print("排查: 1) WDA 服务须在模拟器/真机上运行（复现命令）")
            print("      2) `curl http://localhost:8100/status` 应返回 JSON  3) 真机 usbmux 转发属 P2")
        else:
            print(f"连接设备失败: {type(exc).__name__}: {exc}")
            print("排查: 1) 插上手机并开启 USB 调试、在手机上允许授权  2) `mat devices` 确认在线")
            print("      3) 多台设备时用 --serial 指定  4) WiFi 调试需先 `adb connect <ip:port>`")
        return None


def cmd_devices(_args) -> int:
    devices = list_adb_devices()
    if not devices:
        print("未发现在线设备：检查 USB 连接、USB 调试授权与 adb")
        return 1
    for d in devices:
        print(f"{d['serial']}\t{d.get('model', '?')}\t{d['state']}")
    return 0


def cmd_gen(args) -> int:
    usage = UsageStore()
    try:
        usage.ensure_capacity() # 只预检不核销：模型失败不该扣额度（T53b）
    except Exception as exc:  # QuotaExceeded：免费额度用尽（公平使用政策）
        print(str(exc))
        return 1
    client = GenClient.from_env()
    app_info = {"package": args.package}
    controls = _controls_for(args.package)
    if controls:
        app_info["controls"] = controls
        print("已注入页面索引控件清单（防幻觉）")
    graph = _graph_context_for(args.package)
    if graph:
        app_info["graph"] = graph
        print("已注入页面结构上下文（GEN_GRAPH_CONTEXT=1）")

    if args.calibrate:
        calibration_driver = _connect_driver(args.serial, getattr(args, "platform", "android"))
        if calibration_driver is None:
            return 1
        calibration_dir = Path("artifacts/calibration")
        runner = Runner(calibration_driver, calibration_dir)
        case, history = client.generate_calibrated(
            args.nl, app_info, executor=runner.run, max_rounds=args.max_rounds
        )
        for record in history:
            failed_desc = "; ".join(
                f"{f['step']}({f['category']})" for f in record["failed"]
            ) or "-"
            print(f"校准第 {record['round']} 轮: {record['status']}（失败步骤: {failed_desc}）")
    else:
        case = client.generate(args.nl, app_info)

    Path(args.out).write_text(dump_dsl(case), encoding="utf-8")
    snapshot = usage.consume()  # 用例落盘成功才核销（失败白扣过用户 2 次的教训）
    limit_text = snapshot["gen_limit"] if snapshot["gen_limit"] is not None else "不限"
    _print_gen_quality(case)
    print(f"已生成 {args.out}（{len(case.steps)} 步）——请人工校对步骤与断言后再执行")
    print(f"本月生成用量: {snapshot['gen_count']}/{limit_text}（mat status 查看详情）")
    return 0


def _print_gen_quality(case) -> None:
    """打印生成产物的质量闸门结论（**与导入路径同一套判据**，见 case_quality.py）。

    为什么非打不可：闸门只写进 `meta.quality_issues` 的话，人不会去翻 JSON ——
    而这些问题里有一部分是**确定性必失败**（缺定位断言、不可见字符定位、没切到被测 App）。
    实测 52 条跑测的 37 条失败里 73% 属这类，跑一次真机要几分钟到十几分钟。
    """
    issues = [str(i) for i in (case.meta.get("quality_issues") or [])]
    if not issues:
        return
    print(f"\n⚠️ 质量闸门报出 {len(issues)} 条问题（**建议先改再跑**，其中多数是必失败）：")
    for item in issues:
        print(f"   · {item}")
    print("   （已同时写入用例 meta.quality_issues，便于事后归因）")


def _select_datasets(datasets: list[dict], spec: str | None) -> list[dict] | None:
    """按 --dataset 选择数据组（CASE-06）。

    None / 空 / "all" = 全部（默认行为）；否则逗号分隔的**组名或 1 基序号**（如 `alice` / `1,3`）。
    返回选中的组；无任何匹配返回 None（调用方报错退出，避免静默跑空）。
    """
    if not spec or spec.strip().lower() == "all":
        return list(datasets)
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    picked = [
        ds for idx, ds in enumerate(datasets, 1)
        if str(ds.get("name") or f"组{idx}") in wanted or str(idx) in wanted
    ]
    return picked or None


def _blocked_preconditions(case, allow: bool) -> list[str]:
    """MAT **按构造无法置备**的前置条件（`--allow-unmet-preconditions` 时返回空）。

    为什么默认**拒跑**而不是照跑：跑下去只会产出一条**假的定位失败**，而人会去查定位链 ——
    排查方向从一开始就错了。实测：某资讯类客户端「位置权限尚未授予」那两条永远红。
    见 `preconditions.py` 的模块说明。
    """
    if allow:
        return []
    from .preconditions import unprovisionable

    return unprovisionable(list(case.preconditions or []))


def _print_preconditions(case) -> None:
    """运行前把前置条件打出来 —— 它是**设备状态**，跑挂了要第一时间能对上。"""
    if case.preconditions:
        print("前置条件: " + "、".join(case.preconditions))


def _refused_entry(case, artifacts: Path, blocked: list[str]):
    """把"拒跑"记成一条 `skipped` 的 RunResult + 一份最小报告（聚合页里可见）。

    ⚠️ 记成 `skipped` 而不是 `failed`：**它没跑**，把它算成失败会污染通过率，
    也会让人以为"用例写错了但能跑"。它需要的是**修用例或补前置**，不是查失败原因。
    """
    from .preconditions import refusal_message
    from .report import _CSS, _esc
    from .runner import RunResult, StepResult

    artifacts.mkdir(parents=True, exist_ok=True)
    reason = "前置条件 MAT 无法置备：" + "、".join(blocked)
    run = RunResult(
        case_id=case.case_id, case_name=case.name, status="skipped",
        preconditions=list(case.preconditions or []),
        steps=[StepResult(step_id="precheck", intent="前置条件预检", action="precheck",
                          status="skipped", category="precondition-unmet", error=reason)],
    )
    (artifacts / "report.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>未执行 — {_esc(case.name or case.case_id)}</title><style>{_CSS}</style>"
        "</head><body><h1>未执行（前置条件预检未通过）</h1>"
        f"<pre>{_esc(refusal_message(case.name or case.case_id, blocked))}</pre></body></html>",
        encoding="utf-8")
    return (case.name or case.case_id, f"{artifacts.name}/report.html", run)
def _make_recorder_or_warn(args, artifacts_dir: Path):
    """--record 时构造录屏器；平台/设备不支持时**打印可见警告**并返回 None（诚实降级）。"""
    if not getattr(args, "record", False):
        return None
    from .recording import make_recorder
    recorder, reason = make_recorder(getattr(args, "platform", "android"),
                                     getattr(args, "serial", None), artifacts_dir)
    if recorder is None and reason:
        print(f"⚠️ 录屏不可用：{reason}")
    return recorder


def cmd_run(args) -> int:
    # --app + 目录 = 批量执行某 App 的全部用例
    case_path = Path(args.case)
    if case_path.is_dir() and args.app:
        files = sorted(case_path.glob("*.json"))
        matched = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if (data.get("target") or {}).get("package") == args.app:
                    matched.append(f)
            except (OSError, ValueError):
                continue
        if not matched:
            print(f"目录 {case_path} 中没有 target.package={args.app} 的用例")
            return 1
        suite = {"name": f"app:{args.app}", "cases": [f.as_posix() for f in matched]}
        tmp = Path("artifacts") / f"_suite_{args.app.replace('.', '_')}.json"
        tmp.parent.mkdir(exist_ok=True)
        tmp.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
        args.case, args.artifacts = tmp.as_posix(), f"artifacts/app_{args.app.replace('.', '_')}"
        print(f"批量执行 {len(matched)} 条 {args.app} 用例")
    try:
        kind, payload = load_case_or_suite(Path(args.case))
    except FileNotFoundError:
        print(f"用例文件不存在: {Path(args.case).resolve()}")
        print("首次使用请先在项目根目录执行 `mat init` 生成示例用例；")
        print("或确认当前目录：init 在哪个目录运行，cases/ 就生成在哪里。")
        return 1
    except ValueError as exc:  # 套件结构错误 / DSL 校验错误
        print(f"文件格式错误（{args.case}）: {exc}")
        return 1

    if args.app:
        if kind == "case":
            pkgs = [payload.target.get("package", "")]
        else:
            pkgs = []
            for item in payload.get("cases", []):
                data = item if isinstance(item, dict) else json.loads(Path(item).read_text(encoding="utf-8"))
                pkgs.append((data.get("target") or {}).get("package", ""))
        for pkg in pkgs:
            if pkg != args.app:
                print(f"❌ 用例 target.package={pkg or '(未设置)'} 与 --app {args.app} 不一致，拒绝执行（防止误跑）")
                return 1

    # 前置条件预检**放在连设备之前**：拒跑的用例不该因为"设备没连上"而报成另一种错。
    allow_unmet = getattr(args, "allow_unmet_preconditions", False)
    if kind == "case":
        blocked = _blocked_preconditions(payload, allow_unmet)
        if blocked:
            from .preconditions import refusal_message

            print(refusal_message(payload.name or payload.case_id, blocked), file=sys.stderr)
            return 3          # 3 = 未执行（前置条件预检未通过），与 2（跑了但失败）区分

    driver = _connect_driver(args.serial, getattr(args, "platform", "android"))
    if driver is None:
        return 1

    if kind == "case":
        _print_preconditions(payload)
        root_art = Path(args.artifacts) if args.artifacts else Path("artifacts") / Path(args.case).stem
        if payload.datasets:  # 数据驱动（CASE-06）：每组独立执行一轮
            total = len(payload.datasets)
            selected = _select_datasets(payload.datasets, args.dataset)
            if selected is None:
                names = "、".join(str(d.get("name") or f"组{i}")
                                  for i, d in enumerate(payload.datasets, 1))
                print(f"--dataset {args.dataset} 未匹配任何数据组（可用: {names}）")
                return 1
            if len(selected) != total:
                payload = payload.model_copy(deep=True)
                payload.datasets = selected
                print(f"--dataset {args.dataset}：选中 {len(selected)}/{total} 组")
            results = Runner(driver, root_art).run_datasets(payload, root_art)
            entries = []
            for name, run, artifacts in results:
                render_html(run, artifacts)
                if args.junit_xml:
                    Path(f"{args.junit_xml}.{name}.xml").write_text(render_junit_xml(run), encoding="utf-8")
                entries.append((name, f"{artifacts.name}/report.html", run))
                print(f"[{name}] {run.status}（步骤 {run.step_success_rate:.0%}）")
            render_index_html(payload.name or payload.case_id, entries, root_art)
            passed = sum(1 for _, _, r in entries if r.status == "passed")
            print(f"数据驱动结果: {passed}/{len(entries)} 组通过 · 聚合报告: {(root_art / 'index.html').resolve()}")
            _notify_invite_after_run()
            return 0 if passed == len(entries) else 2
        artifacts = root_art
        run = Runner(driver, artifacts, recorder=_make_recorder_or_warn(args, artifacts)).run(payload)
        report = render_html(run, artifacts)
        if args.junit_xml:
            Path(args.junit_xml).write_text(render_junit_xml(run), encoding="utf-8")
        print(f"结果: {run.status}  步骤成功率 {run.step_success_rate:.0%}  用时 {run.duration_ms}ms")
        if run.failed_categories:
            print(f"失败分类: {run.failed_categories}")
        if run.perf:
            perf_text = "  ".join(f"{k}={v}" for k, v in run.perf.items())
            print(f"性能: {perf_text}")
        print(f"报告: {report.resolve()}")
        from .notify import send_run_finished
        send_run_finished(f"[MAT] {run.case_name or run.case_id}: {run.status}（{run.step_success_rate:.0%}）")
        if args.json:
            print(json.dumps(run.model_dump(exclude={"steps"}), ensure_ascii=False))
        _notify_invite_after_run()
        return 0 if run.status == "passed" else 2

    # 套件模式
    root = Path(args.artifacts) if args.artifacts else Path("artifacts") / Path(args.case).stem
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    refused = 0
    suite_dir = Path(args.case).parent  # 条目相对**套件文件所在目录**解析（manifest 标准语义；
    # 此前按 CWD 解析 ⇒ 套件在 cases/ 里而条目从仓库根数，双入口不同构）
    for index, item in enumerate(payload.get("cases", []), 1):
        if isinstance(item, str):
            case_path = suite_dir / item
            if not case_path.exists():
                case_path = Path(item)  # 回退：CWD 相对（兼容既有写法）
            case = TestCase.model_validate_json(case_path.read_text(encoding="utf-8"))
            stem = case_path.stem
        else:
            case = TestCase.model_validate(item)
            stem = case.case_id
        artifacts = root / f"{index:02d}_{stem}"
        blocked = _blocked_preconditions(case, allow_unmet)
        if blocked:
            # 单条拒跑**不该让整个套件挂掉**：记成 skipped、继续跑后面的，最后单独计数。
            refused += 1
            entries.append(_refused_entry(case, artifacts, blocked))
            print(f"[{index}/{len(payload['cases'])}] {case.name or case.case_id}: 未执行"
                  f"（前置条件 MAT 无法置备：{'、'.join(blocked)}）")
            continue
        # 录屏器（`--record`，来自 feature/console-v2）：平台/设备不支持时打印可见警告并降级
        run = Runner(driver, artifacts,
                     recorder=_make_recorder_or_warn(args, artifacts)).run(case)
        render_html(run, artifacts)
        entries.append((case.name or case.case_id, f"{artifacts.name}/report.html", run))
        pre = f"  前置: {'、'.join(case.preconditions)}" if case.preconditions else ""
        print(f"[{index}/{len(payload['cases'])}] {case.name or case.case_id}: {run.status}"
              f"（步骤 {run.step_success_rate:.0%}）{pre}")

    index_page = render_index_html(payload.get("name", Path(args.case).stem), entries, root)
    passed = sum(1 for _, _, r in entries if r.status == "passed")
    failed = sum(1 for _, _, r in entries if r.status == "failed")
    tail = f"（{refused} 条因前置条件未执行）" if refused else ""
    print(f"套件结果: {passed}/{len(entries)} 通过{tail}  聚合报告: {index_page.resolve()}")
    _notify_invite_after_run()
    if failed:
        return 2
    return 3 if refused else 0


def cmd_import(args) -> int:
    from .manual_import import TemplateError, convert_batch, parse_file, save_import_bundle

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在: {path.resolve()}")
        return 1
    template = None
    if args.template and args.template != "auto":
        try:
            template = json.loads(Path(args.template).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"映射模板读取失败: {exc}")
            return 1
    try:
        cases, used = parse_file(path, template)
    except TemplateError as exc:
        print(f"解析失败: {exc}")
        return 1
    if not cases:
        print("未解析到用例（检查文件内容与映射）")
        return 1
    if args.limit:
        cases = cases[: args.limit]
    print(f"解析到 {len(cases)} 条用例 · 映射: {json.dumps(used, ensure_ascii=False)}")

    # R9b：把「可置备的页面状态」前置条件展开成导航步骤（默认开；--no-expand-preconditions 关）
    if not getattr(args, "expand_preconditions", True):
        os.environ["MAT_EXPAND_PRECONDITIONS"] = "0"
    from .precondition_expand import expand_enabled
    from .preconditions import provisionable

    page_states = sum(1 for c in cases if provisionable(c.preconditions))
    if page_states:
        if expand_enabled():
            print(f"{page_states} 条带「可置备的页面状态」前置条件 → 将展开成导航步骤"
                  f"（多消耗 {page_states * 0.5:g} 次 gen 额度；展开结果带 `【前置展开】` 标记，请人工确认）")
        else:
            print(f"{page_states} 条带「可置备的页面状态」前置条件 → **未展开**"
                  "（执行前需手工把设备置备到该状态）")

    try:
        client = GenClient.from_env()
    except Exception as exc:
        print(str(exc))
        return 1
    controls = _controls_for(args.package)
    if controls:
        print("已注入页面索引控件清单（防幻觉）")
    graph = _graph_context_for(args.package)
    if graph:
        print("已注入页面结构上下文（GEN_GRAPH_CONTEXT=1）")

    def progress(done: int, total: int) -> None:
        print(f"\r转换进度 {done}/{total}", end="", flush=True)

    try:
        batch = convert_batch(client, cases, args.package, UsageStore(), on_progress=progress,
                              controls=controls, graph=graph,
                              index_dir=Path("artifacts") / "page_index")
    except Exception as exc:  # QuotaExceeded 等：批量前预检，不产生半批结果
        print("\n" + str(exc))
        return 1
    print()
    import_id, bundle = save_import_bundle(Path.cwd(), path, args.package, batch, used)
    summary = batch["summary"]
    drafts = sum(1 for r in batch["results"] if r.get("case_file"))
    print(f"转换完成: 共 {summary['total']} 条 → A {summary['A']} / B {summary['B']} / C {summary['C']}"
          f"（产出草稿 {drafts} 条，已写入 cases/）")
    _print_quality(batch.get("quality") or {})
    print(f"批次报告: {(bundle / 'import.json').resolve()}")
    print("下一步: mat console   # 用例页复核草稿 → 试跑")
    return 0


def _print_quality(quality: dict) -> None:
    """打印批次质量口径（R15）。

    ⚠️ **必须说清它不是「可执行率」**：这里算的是"**离线**能判定的缺陷"，
    而「不改就能跑绿的比例」要跑过真机才知道。两个数字混着报，人就会把前者当后者用
    （同族 D10：一个看起来像分数的数字会被当成证据引用）。
    """
    if not quality or not quality.get("total"):
        return
    total, blocked = quality["total"], quality["blocked"]
    print(f"质量（离线口径）: 无阻断缺陷 {quality['no_blocking_rate']:.0%}"
          f"（{total - blocked}/{total}）· A 级（零缺陷）{quality['clean_rate']:.0%}")
    by_rule = quality.get("by_rule") or {}
    if by_rule:
        from .case_quality import RULES

        shown = "、".join(f"{code}×{n}" for code, n in list(by_rule.items())[:6])
        print(f"  按规则命中: {shown}")
        top = next(iter(by_rule))
        if top in RULES:
            print(f"  最多的一类: {top} —— {RULES[top][1]}")
    print("  ⚠️ 它**不是**「可执行率」：「不改就能跑绿的比例」必须与真机跑测数据 join 才算得出")


def cmd_index(args) -> int:
    from .page_index import load_or_crawl

    if getattr(args, "platform", "android") == "ios":
        print("iOS 平台暂不支持 index（页面图谱的 WDA 适配在 P3）——当前 iOS 可用：mat run --platform ios")
        return 1

    driver = _connect_driver(args.serial, getattr(args, "platform", "android"))
    if driver is None:
        return 1
    # A3（2026-10-12 评审）：包名会拼进 artifacts 路径，绝对路径/分隔符在入口就拒。
    from .pkgname import safe_package_name

    try:
        safe_package_name(args.package)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    index_path = Path("artifacts/page_index") / f"{args.package}.json"
    index, created = load_or_crawl(driver, args.package, index_path, refresh=args.refresh,
                                   max_steps=args.steps, max_pages=args.pages)
    pages = index.get("pages", [])
    controls = sum(p.get("node_count", 0) for p in pages)
    print(f"{'已构建' if created else '已有'}图谱: {index_path.resolve()}")
    print(f"页面: {len(pages)} · 控件: {controls} · schema v{index.get('schema', 1)}"
          f" · 更新: {index.get('updated_at') or index.get('generated_at', '-')}")
    if controls == 0:
        # **空图谱是静默失败的典型**：退出码 0、输出看着正常，但图谱里一个控件都没有，
        # 下游 gen 的防幻觉底座已经废了。实测踩过（B2）——所以这里必须响亮报错。
        # crawl 现已自动拉起被测 App（page_index._ensure_foreground），走到这里说明
        # 拉起后仍收不到该包控件——多半是锁屏、App 崩溃秒退或包名错。
        print("⚠️ 图谱为空（0 控件）：下游 gen 的防幻觉底座将失效。已自动尝试拉起被测 App 仍失败——常见原因与处理：", file=sys.stderr)
        print("   1) 设备锁屏或弹窗遮挡——解锁后重跑；", file=sys.stderr)
        print("   2) App 启动即崩溃/秒退——先手动打开确认可用；", file=sys.stderr)
        print("   3) 包名写错——用 `adb shell pm list packages | grep <关键字>` 核对。", file=sys.stderr)
        return 2
    print("下一步: mat gen ... --package <同包名>  # 生成时自动注入控件清单（防幻觉）")
    return 0


def _controls_for(package: str) -> str:
    """读取既有页面图谱 → prompt 控件清单（无图谱返回空串，gen/convert 共用）。"""
    from .page_index import controls_context_for

    return controls_context_for(package)


def _graph_context_for(package: str, max_pages: int = 12) -> str:
    """图谱 → 注入 prompt 的页面结构上下文（严格限量防 prompt 膨胀）。

    默认**关闭**，`GEN_GRAPH_CONTEXT=1` 开启：这是会改变 AI 生成输出的增强，
    应先做真机 A/B 对照再决定是否默认打开（诚实边界）。
    """
    from .page_index import graph_context_for

    return graph_context_for(package, max_pages=max_pages)


def cmd_explore(args) -> int:
    from .explore import explore as _explore
    from .report import render_explore_html

    if getattr(args, "platform", "android") == "ios":
        print("iOS 平台暂不支持 explore（页面图谱的 WDA 适配在 P3）——当前 iOS 可用：mat run --platform ios")
        return 1

    driver = _connect_driver(args.serial, getattr(args, "platform", "android"))
    if driver is None:
        return 1
    # A3 同口径（见 cmd_index）：包名形状非法在入口就拒。
    from .pkgname import safe_package_name

    try:
        safe_package_name(args.package)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    import uuid as _uuid
    explore_id = datetime.now(UTC).strftime("%H%M%S") + "-" + _uuid.uuid4().hex[:4]
    artifacts = Path("artifacts/explore") / explore_id
    index_path = Path("artifacts/page_index") / f"{args.package}.json"
    print(f"探索中：{args.package}（预算 {args.minutes} 分钟 / {args.steps} 步 / 深度 ≤L{args.max_depth}，敏感控件自动跳过）…")
    report = _explore(driver, args.package, minutes=args.minutes, max_steps=args.steps,
                      artifacts_dir=artifacts, max_depth=args.max_depth, index_path=index_path)
    render_explore_html(report, artifacts)
    depth_line = " · ".join(f"{k}:{v}" for k, v in
                            sorted(report.get("depth_stats", {}).items(),
                                   key=lambda kv: int(kv[0][1:])))
    dedup = report.get("dedup_merges", 0)
    dedup_note = f" · 同屏去重 {dedup} 帧" if dedup else ""
    # 结束原因必须印出来："探完了"与"时间到了"对用户的含义完全不同
    # （前者可以停手，后者说明该加预算）。
    # ⚠️ `stuck` / `blocked` **必须带上成因**（T69）：处置方向完全不同 ——
    # `depth-limit` 要调大 `--max-depth`，而 `no-candidates` 调大**也没用**；
    # `blocked` 更不是"预算不够"（实测一轮 72.9s 就报过"预算耗尽"，真因是离场拉不回）。
    _stop_txt = {"exhausted": "已探完", "budget": "预算耗尽", "blocked": "被挡住"}
    _cause_txt = {"depth-limit": "触到深度上限", "no-candidates": "候选耗尽",
                  "left-streak": "离场拉不回", "fallback-failed": "回退失败",
                  "never-started": "App 没启动起来", "risk-control": "撞到风控/验证页"}
    _stop = _stop_txt.get(report.get("stop_reason"), report.get("stop_reason") or "—")
    _cause = report.get("stuck_cause") or report.get("blocked_cause")
    if _cause:
        _stop += f"（{_cause_txt.get(_cause, _cause)}）"
    stop_note = f" · 结束 {_stop}"
    print(f"探索完成: 步数 {report['steps']} · 页面 {report['page_count']}（{depth_line}）"
          f" · 跳转 {len(report.get('edges', []))} 条{dedup_note} · 发现 {len(report['findings'])} 个"
          f" · 选点 {'图谱启发式' if report.get('heuristic', True) else '纯轮转(对照)'}{stop_note}")
    if report.get("prior_pages"):
        # 三个数字的关系必须说清，否则又变成"读回 = 起了作用"的老错觉：
        #   读回   = 图谱里**全部**页面的已试分支数（分母很大，与"起了多少作用"无关）
        #   检查过 = 本轮**实际走到的**页面上的候选槽位数（这才是可比的分母）
        #   跳过   = 其中因历史而没点的
        # 跳过率 = 跳过 / 检查过（**同口径**；用读回量做分母是错配）
        loaded = report.get("reused_tried", 0)
        skipped = report.get("skipped_by_history", 0)
        considered = report.get("candidates_considered", 0)
        rate = report.get("skip_rate")
        rate_txt = f"{rate:.0%}" if rate is not None else "—"
        line = (f"增量: 图谱已有 {report['prior_pages']} 页 · 读回 {loaded} 个已试分支 · "
                f"本轮检查 {considered} 个候选，其中因历史跳过 {skipped} 个（跳过率 {rate_txt}）")
        # 告警只在**强信号**下触发：读回了历史、也确实检查过候选，却一个都没跳。
        # 不加 `considered` 这个前提的话，空转轮（没走到任何已知页）会误报。
        if loaded and considered and skipped == 0:
            line += "（⚠️ 读回了历史分支且检查过候选，却一个都没跳——包含度认页疑似失效）"
        print(line)
    if report.get("index_path"):
        print(f"已合并入页面图谱: {Path(report['index_path']).resolve()}")
    risky = report.get("risky_pages") or []
    if risky:
        print(f"高风险页: {len(risky)} 个（历史发现过崩溃/白屏/离场——下轮探索将优先回探这些页的入口）")
    for f in report["findings"]:
        print(f"  [{f['type']}] {f.get('detail', '')[:100]}")
    print(f"报告: {(artifacts / 'report.html').resolve()}")
    return 0


def cmd_diagnose(args) -> int:
    """对一次执行的失败步骤做 LLM 归因（BYOK/server 双模式；失败自动回退规则结果）。"""
    from .diagnose import diagnose_run
    from .report import render_html
    from .runner import RunResult

    run_dir = Path(args.run_dir)
    run_json = run_dir / "run.json"
    if not run_json.exists():
        print(f"未找到 {run_json}——用法: mat diagnose artifacts/<产物目录>")
        return 1
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
        run = RunResult.model_validate(data["run"])
    except (ValueError, KeyError) as exc:
        print(f"run.json 解析失败: {exc}")
        return 1
    failed = [s for s in run.steps if s.status != "passed"]
    if not failed:
        print("本次执行无失败步骤，无需归因")
        return 0

    from .ai_client import GenClient

    client = GenClient.from_env()
    mode = "server" if getattr(client, "prompt_mode", "local") == "server" else (
        "BYOK" if client.api_key else "无 Key")
    print(f"归因中: {len(failed)} 个失败步骤（模式: {mode}）…")
    attributions = diagnose_run(run, run_dir, client)

    (run_dir / "diagnose.json").write_text(
        json.dumps(attributions, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html(run, run_dir)  # 重渲染报告，挂上归因卡
    print()
    for s in failed:
        a = attributions.get(s.step_id, {})
        label = {"app-bug": "真 bug", "ui-change": "UI 改版", "env-flake": "环境抖动",
                 "precondition": "前置缺失"}.get(a.get("category", ""), a.get("category", "—"))
        conf = f"{a['confidence']:.0%}" if a.get("confidence") is not None else "—"
        flag = " ⚠ 建议人工确认" if a.get("need_human") else ""
        print(f"  [{s.step_id}] {label} · 置信度 {conf}{flag}")
        if a.get("suggestion"):
            print(f"      建议: {a['suggestion']}")
        for ev in a.get("evidence", []):
            print(f"      证据: {ev}")
    print(f"\n归因已写入 {run_dir / 'diagnose.json'}，报告已更新（失败步骤挂归因卡）")
    return 0


def cmd_arrival(args) -> int:
    """arrival 试跑验证：图谱路径重放 N 轮，通过 ≥N-1 才算可靠。"""
    from .arrivals import save_report, verify_arrivals

    if getattr(args, "platform", "android") == "ios":
        print("iOS 平台暂不支持 arrival（依赖页面图谱，适配在 P3）——当前 iOS 可用：mat run --platform ios")
        return 1

    driver = _connect_driver(args.serial, getattr(args, "platform", "android"))
    if driver is None:
        return 1
    import uuid as _uuid
    index_path = Path("artifacts/page_index") / f"{args.package}.json"
    if not index_path.exists():
        print(f"没有页面图谱: {index_path.resolve()}——请先 mat index / mat explore")
        return 1
    print(f"试跑验证：{args.package}（每条路径 {args.trials} 轮，最多 {args.max_paths} 条，"
          f"通过 ≥{args.trials - 1} 轮记 stable）…")
    report = verify_arrivals(driver, args.package, index_path, trials=args.trials,
                             max_paths=args.max_paths, sleep_scale=1.0)
    out = save_report(report, Path("artifacts/arrival")
                      / (datetime.now(UTC).strftime("%H%M%S") + "-" + _uuid.uuid4().hex[:4]))
    print(f"验证完成: 共 {report['verified']} 条 · stable {report['stable']} 条 · "
          f"点击 {report['clicks']} 次"
          + ("（点击预算提前收敛）" if report["stopped_early"] else ""))
    for r in report["results"]:
        mark = {"stable": "✓", "unstable": "✗", "skipped": "–"}.get(r["status"], "?")
        print(f"  {mark} L{r.get('depth', '?')} {r['page_key'][:8]} "
              f"{r.get('rounds', '-')}轮过{r.get('passed', '-')} · {r.get('reason', '')[:80]}")
    print(f"报告: {out.resolve()}")
    return 0


def cmd_describe(args) -> int:
    """给图谱里的页面补「标题 + 功能描述」（可选增强）。

    一次 AI 调用描述一批（默认 ≤20 页），结果写回图谱并标注 `*_source="ai"`；
    已有人工标题（`manual`）不会被顶掉。**这是唯一会花额度的图谱命令**——
    其余图谱能力（层级 / tried / 结构 / 导航边）全部零 AI 成本。
    """
    from .page_index import load_index, save_index, update_page

    index_path = Path("artifacts/page_index") / f"{args.package}.json"
    index = load_index(index_path)
    if index is None:
        print(f"没有页面图谱: {index_path}")
        print(f"先构建: mat index {args.package}"
              f"（或 explore 自动沉淀）")
        return 1

    pages = index.get("pages", [])
    if not pages:
        print("图谱里还没有页面（先 index 或 explore）")
        return 1
    targets = [p for p in pages if args.refresh or p.get("summary_source") not in ("ai", "manual")]
    targets = [p for p in targets if p.get("title_source") != "manual"]
    if not targets:
        print("所有页面都已有 AI 描述（--refresh 强制重算；人工标题不会被覆盖）")
        return 0
    targets = targets[: args.limit]

    try:
        usage = UsageStore()
        usage.ensure_capacity() # 只预检不核销（T53b）：此前模型失败也扣额度（白扣 2 次）
    except Exception as exc:  # QuotaExceeded：免费额度用尽（公平使用政策）
        print(str(exc))
        return 1

    payload = [
        {
            "page_key": p.get("page_key"),
            "depth": p.get("depth"),
            "parent_action": p.get("parent_action"),
            "activity": p.get("activity", ""),
            "key_elements": (p.get("structure") or {}).get("key_elements", []),
            "layout_regions": (p.get("structure") or {}).get("layout_regions", []),
            "dynamic_regions": (p.get("structure") or {}).get("dynamic_regions", []),
        }
        for p in targets
    ]
    print(f"描述 {len(payload)} 个页面（1 次 AI 调用，计 1 次生成额度）…")
    try:
        described = GenClient.from_env().describe_pages(payload)
    except Exception as exc:
        print(f"描述失败: {exc}")
        return 1
    snapshot = usage.consume()  # 描述成功落盘前才核销（T53b）

    updated = 0
    for page in targets:
        info = described.get(page.get("page_key"))
        if not info or not info.get("title"):
            continue
        update_page(index, page["page_key"], title=info["title"], title_source="ai",
                    summary=info.get("summary", ""), summary_source="ai")
        updated += 1
    save_index(index_path, index)

    limit_text = snapshot["gen_limit"] if snapshot["gen_limit"] is not None else "不限"
    print(f"已补 {updated}/{len(targets)} 个页面 → {index_path}")
    for page in targets[:10]:
        if described.get(page.get("page_key")):
            print(f"  L{page.get('depth')} {described[page['page_key']]['title']}"
                  f" —— {described[page['page_key']]['summary']}")
    print(f"本月生成用量: {snapshot['gen_count']}/{limit_text}")
    return 0


def vision_status_text(*, server: bool, key_ok: bool, vl: str | None) -> str:
    """视觉兜底状态文案。

    注意：**"配了模型" ≠ "会触发"**。计费兜底默认关闭，配了 `GEN_VL_MODEL` 只是具备条件，
    还要步骤授权或全局开关。文案必须把这点说清，否则用户会以为配了就自动生效。
    """
    usable = bool(key_ok and (server or vl))
    if not usable:
        return f"未配置（配 GEN_VL_MODEL 后仍需显式授权；{VISION_OPT_IN_HINT}）"
    if os.environ.get("GEN_VL_FALLBACK") == "1":
        where = "轻云，模型服务端决定" if server else vl
        return f"已启用（全局授权 GEN_VL_FALLBACK=1 · {where}）"
    return f"已配置（{vl or '轻云'}）但**默认不触发**：{VISION_OPT_IN_HINT}"


def cmd_review(args) -> int:
    """人工复核页面标题：生成复核页 / 应用结论 / 重放（见 `review.py` 的模块说明）。

    三种用法（`package` 始终必填）：

        mat review <包名>                    # 渲染复核页，改完导出 JSON
        mat review <包名> --apply <文件>      # 应用导出的结论（落盘 + 应用到图谱）
        mat review <包名> --replay           # 从 review/<包名>.json 重放（图谱重建后用）

    ⚠️ **为什么结论要单独落盘**：图谱是派生物（重跑 `explore` 会覆盖它，人工标题会丢），
    而复核结论是**用户资产** ⇒ 存到 `<工作区>/review/<包名>.json`，与图谱解耦。
    """
    from . import review
    from .page_index import save_index

    root = Path.cwd()
    pkg = args.package
    try:
        index = review.load_index(pkg, root)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1

    # ---- 应用 / 重放 ----
    if args.apply or args.replay:
        src = Path(args.apply) if args.apply else review.review_path(pkg, root)
        if not src.exists():
            print(f"找不到复核结论：{src}")
            print(f"先跑 `mat review {pkg}` 生成复核页，改完导出后再 `--apply <文件>`。")
            return 1
        try:
            loaded = review.load_corrections(src)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"复核结论读不动（{src}）：{exc}")
            return 1
        # **防止改错 App**：结论文件里记了包名就核对一下
        if loaded["package"] and loaded["package"] != pkg:
            print(f"⚠️ 这份结论是给 {loaded['package']} 的，与 {pkg} 不符 ⇒ 中止（防改错 App）")
            return 1
        corrections = loaded["corrections"]
        if not corrections:
            print("这份结论里没有修改。")
            return 1

        stats = review.apply_corrections(index, corrections)
        if stats["applied"]:
            save_index(review.index_path(pkg, root), index)
        if args.apply:
            # `--apply` 把结论**累积**落盘（多次复核不互相覆盖；同 key 以新值胜）
            store = review.review_path(pkg, root)
            existing: dict = {}
            if store.exists() and src != store:
                try:
                    existing = review.load_corrections(store)["corrections"]
                except (ValueError, json.JSONDecodeError):
                    print(f"⚠️ 已有的结论存档读不动（{store}），本次会覆盖它")
            existing.update(corrections)
            review.save_review(pkg, existing, root,
                               note=f"由 mat review --apply 累积写入（源 {src.name}）")
        print(f"复核结论已应用：改 {len(stats['applied'])} 页 · "
              f"已是目标值 {len(stats['unchanged'])} 页 · 找不到 {len(stats['missing'])} 页")
        if stats["missing"]:
            # ⚠️ **逐条报出来**：图谱重跑后 `page_key` 可能变化，静默丢弃等于用户白复核
            print(f"⚠️ 以下 {len(stats['missing'])} 个 page_key 在图谱里找不到（图谱可能被重跑过）：")
            for k in stats["missing"][:10]:
                print(f"     {k}")
            if len(stats["missing"]) > 10:
                print(f"     …另有 {len(stats['missing']) - 10} 个")
        print(f"结论存档：{review.review_path(pkg, root).relative_to(root)}"
              f"（图谱重建后用 `mat review {pkg} --replay` 恢复）")
        return 0

    # ---- 生成复核页 ----
    items = review.build_items(index)
    if not items:
        print(f"{pkg} 的图谱里还没有页面。先跑 `mat index {pkg}` 或 `mat explore {pkg}`。")
        return 1
    out = review.html_path(pkg, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(review.render_html(pkg, items, root), encoding="utf-8")
    named = sum(1 for it in items if it["title"])
    print(f"复核页已生成：{out}")
    print(f"  共 {len(items)} 页 · 已有标题 {named} 页"
          f"{'（其余还没跑过 describe）' if named < len(items) else ''}")
    print("  在浏览器里打开它，改完标题点「导出复核结论」，然后：")
    print(f"     mat review {pkg} --apply <下载的文件>")
    return 0


def cmd_calibrate(args) -> int:
    """报告校准：生成校准页 / 应用改判（并落成规则）/ 查看规则表（见 `calibration.py`）。

        mat calibrate artifacts/<产物目录>              # 渲染校准页
        mat calibrate artifacts/<产物目录> --apply <文件>  # 应用：改报告 + 落规则
        mat calibrate --show-rules                      # 列出累积的规则

    ⚠️ **为什么要有「落成规则」这一步**：只改这一次的报告等于白改 —— 下一次 run 会
    一模一样地再判错（与「复核只兑现一次」是同一个坑）。
    """
    from . import calibration

    root = Path.cwd()

    if args.show_rules:
        rules = calibration.load_rules(root)
        path = calibration.rules_path(root)
        if not rules:
            print(f"还没有校准规则（{path} 不存在或为空）。")
            print("先跑 `mat calibrate artifacts/<产物目录>`，改判时填上规则条件。")
            return 0
        print(f"共 {len(rules)} 条规则（{path}）：")
        for i, r in enumerate(rules, 1):
            w = r.get("when") or {}
            cond = " 且 ".join(filter(None, [
                f"判定为 {w.get('category')}" if w.get("category") else "",
                f"错误含「{w.get('error_contains')}」" if w.get("error_contains") else "",
                f"日志含「{w.get('logcat_contains')}」" if w.get("logcat_contains") else ""]))
            print(f"  {i}. 当 {cond} ⇒ 改判为 "
                  f"{calibration.CATEGORY_LABELS.get(r.get('set_category'), r.get('set_category'))}")
            if r.get("why"):
                print(f"     理由: {r['why']}")
            bad = calibration.validate_rule(r)
            if bad:
                print(f"     ⚠️ 这条规则**非法会被跳过**：{bad}")
        return 0

    run_dir = Path(args.run_dir)
    run_json = run_dir / "run.json"
    if not run_json.exists():
        print(f"未找到 {run_json}——用法: mat calibrate artifacts/<产物目录>")
        return 1
    try:
        payload = json.loads(run_json.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"run.json 解析失败: {exc}")
        return 1

    # ---- 应用校准结论 ----
    if args.apply:
        src = Path(args.apply)
        if not src.exists():
            print(f"找不到校准结论：{src}")
            return 1
        try:
            cal = calibration.load_calibration(src)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"校准结论读不动（{src}）：{exc}")
            return 1
        # **防止应用到别的 run**：结论里记了产物目录就核对一下
        claimed = str(cal.get("run") or "")
        if claimed and Path(claimed).name != run_dir.name:
            print(f"⚠️ 这份校准结论是给 {claimed} 的，与 {run_dir} 不符 ⇒ 中止（防改错报告）")
            return 1

        stats = calibration.apply_calibration(payload, cal)
        if stats["applied"]:
            run_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            # 重渲染报告，让改判与「人工」标记显示出来（照 cmd_diagnose 的做法）
            from .runner import RunResult

            try:
                render_html(RunResult.model_validate(payload["run"]), run_dir)
            except Exception as exc:  # noqa: BLE001 —— 报告重渲染失败不该让校准白做
                print(f"⚠️ 报告重渲染失败（run.json 已改）：{type(exc).__name__}: {exc}")
        print(f"校准已应用：改判 {len(stats['applied'])} 步 · 判定一致 {len(stats['unchanged'])} 步 · "
              f"找不到 {len(stats['missing'])} 步")
        if stats["missing"]:
            print(f"⚠️ 以下 step_id 在本次 run 里找不到：{'、'.join(stats['missing'][:10])}")
        if stats["rules"]:
            path = calibration.save_rules(root, stats["rules"],
                                          source=f"mat calibrate --apply（源 {src.name}）")
            print(f"已落成 {len(stats['rules'])} 条**规则** → {path.relative_to(root)}")
            print("  ⇒ 以后同类失败会自动按你的判定归类（`mat calibrate --show-rules` 可查）")
        elif not stats["assertion_fixes"]:
            # 只提示"没落规则"，但**做了断言校准**时这话就不对（那才是让结果变对的那层）
            print("  （没有填规则条件 ⇒ 只改了这一次的报告的**分类**，同类失败下次仍会判错；"
                  "要「不再犯」请填「错误里包含」或「日志里包含」）")
        # ---- 断言校准：改**用例**（让结果本身正确），照 heal 的先例不覆盖原用例 ----
        if stats["assertion_fixes"]:
            case_id = str((payload.get("run") or {}).get("case_id") or "")
            case_file = calibration.find_case_file(root, case_id)
            if case_file is None:
                print(f"⚠️ 断言校准有 {len(stats['assertion_fixes'])} 条，但没在 cases/ 里找到 "
                      f"caseId={case_id!r} 的用例 ⇒ **跳过，没写任何文件**")
            else:
                try:
                    case = json.loads(case_file.read_text(encoding="utf-8"))
                except ValueError as exc:
                    print(f"⚠️ 用例读不动（{case_file}）：{exc}")
                    case = None
                if case is not None:
                    patched, notes = calibration.patch_case(case, stats["assertion_fixes"])
                    out = case_file.with_name(f"{case_file.stem}-calibrated.json")
                    out.write_text(json.dumps(patched, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
                    print(f"已写出**校准后的用例**：{out.relative_to(root)}"
                          f"（**不覆盖原用例**，照 `mat heal` 的先例）")
                    for note in notes:
                        print(f"    {note}")
                    print(f"  ⇒ 下次跑 `mat run {out.relative_to(root)}` 验证是否真的通过")
        # 存档：结论与产物目录放在一起，便于回溯
        try:
            calibration.calibration_path(run_dir).write_text(
                json.dumps(cal, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        except OSError:
            pass
        return 0

    # ---- 生成校准页 ----
    items = calibration.build_items(payload, run_dir)
    if not items:
        print("本次执行没有失败步骤，无需校准。")
        return 0
    out = calibration.html_path(run_dir)
    # 传**完整路径**而不是目录名：页面上的下一步命令要能直接复制粘贴
    out.write_text(calibration.render_html(str(run_dir), items), encoding="utf-8")
    calibrated = sum(1 for it in items if it["category_source"] == "human")
    print(f"校准页已生成：{out}")
    print(f"  失败步骤 {len(items)} 个"
          f"{f'（其中 {calibrated} 个已人工改判过）' if calibrated else ''}")
    print("  在浏览器里打开它，对着证据改判；**想「下次不再犯」就填上规则条件**。然后：")
    print(f"     mat calibrate {run_dir} --apply <下载的文件>")
    return 0


def cmd_doctor(_args) -> int:
    """环境自检:新用户卡点的第一道诊断(检查逻辑在 doctor.py,此处只渲染)。

    F2 重构:检查与展示分离——CLI 逐行打 ✅/❌(格式与重构前一致),
    配置台走 /api/doctor 渲染红黄绿卡片,两处同一份数据。
    """
    from . import doctor

    def render(checks: list) -> None:
        for c in checks:
            mark = "✅" if c.ok else "❌"
            print(f"{mark} {c.label}")
            if not c.ok:
                print(f"    └─ {c.hint}")

    android = doctor.android_checks()
    render(android)
    # iOS(P1 模拟器 MVP):可选能力——缺失不影响 Android 使用,不计入退出码
    print("── iOS（可选，缺失不影响 Android 使用）──")
    render(doctor.ios_checks())

    critical = sum(1 for c in android[:4] if not c.ok)
    print(f"\n结论: {'就绪，可以开测' if critical == 0 else f'{critical} 项关键检查未通过（前 4 项）'}")
    return 0 if critical == 0 else 1


def _register_activation(licensing, code: str) -> None:
    """把激活登记到服务端（T17）—— **best-effort，绝不阻断本地激活**。

    本地 `license.json` 是离线验签写盘的；服务端那一步换来的是**服务端额度**
    （300 次/月真实计量）与机器绑定。断网时本地照旧可用（BYOK 零成本），只是拿不到
    服务端额度 —— 这正是 红线的形态：本地码不兑服务端额度，服务端额度只跟 token 走。
    """
    try:
        out = licensing.activate_on_server(code)
    except licensing.CloudError as exc:
        if getattr(exc, "status", None) and 400 <= exc.status < 500:
            # 🔴 服务端**明确判定**（典型是超限 409）：重跑同一条命令结果**不会变**。
            # 原先这里一律说"之后重跑即可" —— 对超限是**错的方向**，用户会一直重试。
            print(f"⚠️ 本地授权已生效（有效期见上），但服务端额度**没开通**：{exc}")
            print("  这是服务端的判定，重跑这条命令结果不变 —— "
                  "换机请先在原机器上执行 `mat activate --unbind`，或联系我们处理。")
        else:
            print(f"（服务端登记未完成：{exc}）")
            print("  本地授权已生效、不影响使用；服务端额度需要联网登记，"
                  "之后重跑 `mat activate <激活码>` 即可。")
        return
    print(f"✅ 服务端额度已开通（本码已绑定 {out.get('machines')}/{out.get('max_machines')} 台机器）")
    if out.get("reused"):
        print("  （本机此前激活过，凭证已重新签发）")
    print("  用官方代理：把下面一行加进工作区 .env"
          "（另需 GEN_BASE_URL / GEN_PROMPT_MODE=server）：")
    print(f"    GEN_API_KEY={out['token']}")
    print("  换机/重装：`mat activate --machines` 看已绑机器，"
          "`mat activate --unbind <机器标识>` 解绑")


def _print_bound_machines(licensing) -> int:
    try:
        out = licensing.list_server_machines()
    except licensing.CloudError as exc:
        print(f"查询失败：{exc}", file=sys.stderr)
        return 1
    machines = out.get("machines", [])
    print(f"本激活码已绑定 {len(machines)}/{out.get('max_machines')} 台机器：")
    for m in machines:
        mark = "   ← 本机" if m.get("machine_id") == out.get("self") else ""
        print(f"  {m.get('machine_id')}  （{m.get('created_at', '')}）{mark}")
    return 0


def cmd_seat(args) -> int:
    """团队成员：本机标识 / 同步配额 / 查看现状（2026-10-14）。

    团队成员数（原「席位」）是**服务端按主机发的配额**（`seat_quota`），与激活码（授权）
    是两个独立维度：客户不需要改任何配置 —— 把标识发给运营，运营加完，这边 `sync`
    一次就生效；各批配额各自带到期时间，到点自动回落（离线也成立，见 `seat_quota`）。
    """
    from . import licensing, seat_quota

    action = getattr(args, "seat_action", None) or "status"

    if action == "id":
        mid = licensing.machine_id()
        print("本机标识（把这行发给运营，用于给这台机器增加成员名额）：")
        print(f"  {mid}")
        print(f"  ↑ 也可以只报前 12 位：{mid[:12]}（唯一性足够）")
        return 0

    if action == "sync":
        from .cloudapi import CloudError

        try:
            seat_quota.sync_from_server()
        except CloudError as exc:
            print(f"同步失败：{exc}", file=sys.stderr)
            print("（成员名额不会因为断网丢掉：本地缓存里每一批都记着自己的到期时间）",
                  file=sys.stderr)
            return 1
        print(f"✅ 已同步\n{seat_quota.describe()}")
        return 0

    print(seat_quota.describe())
    return 0


def cmd_activate(args) -> int:
    from . import licensing

    if getattr(args, "machines", False):
        return _print_bound_machines(licensing)
    if getattr(args, "unbind", None):
        try:
            out = licensing.unbind_on_server(args.unbind)
        except licensing.CloudError as exc:
            print(f"解绑失败：{exc}", file=sys.stderr)
            return 1
        print(f"✅ 已解绑 {out.get('unbound')} —— 那台机器现在可以重新激活了。")
        return 0

    if not getattr(args, "code", None):
        print("用法：mat activate <激活码> ｜ mat activate --machines ｜ mat activate --unbind <标识>",
              file=sys.stderr)
        return 1
    try:
        info = licensing.activate(args.code)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    import time as _t

    def _day(ts) -> str:
        return _t.strftime("%Y-%m-%d", _t.localtime(ts))

    if info.get("kept_existing"):
        # 本机已有不更短的授权 ⇒ **一个字节都没改**。此时不能再打 "✅ 已激活"：
        # 用户会以为自己又续上了一次（这正是 2026-10-13 要修的表达缺陷）。
        # 三种子情形必须分开说 —— 尤其第 3 种是**活动码用户的高频处境**：
        # 他领了第二枚 `--until` 同一天的码，两枚截止日一样，"已保留更长的那个"是错话。
        current, code_exp = info["expires_at"], info["code_expires_at"]
        if info.get("same_code"):
            print(f"ℹ️ 你已经用这张激活码激活过本机了（有效期至 {_day(current)}）"
                  "——本次未做任何改动。")
        elif code_exp < current:
            print(f"ℹ️ 这张码的截止日（{_day(code_exp)}）早于本机现有授权"
                  f"（{_day(current)}）——已保留更长的那个，本次未做任何改动。")
        else:
            print(f"ℹ️ 本机已有同等有效期的授权（至 {_day(current)}），这张码不必再用"
                  "——本次未做任何改动。")
    else:
        print(f"✅ 已激活 {info['plan']}（有效期至 {_day(info['expires_at'])}）")
    if info.get("redeemed"):
        # 时长卡：核销已在 activate() 内完成（服务端登记 + 重签普通码写盘），无需再登记
        token = licensing.read_server_token()
        print("✅ 时长卡已在服务端核销，有效期以核销结果为准（本机已换发普通激活码）。")
        if token:
            print("  用官方代理：把下面一行加进工作区 .env"
                  "（另需 GEN_BASE_URL / GEN_PROMPT_MODE=server）：")
            print(f"    GEN_API_KEY={token}")
        return 0
    if info.get("offline_fallback"):
        print(f"（{info.get('note', '')}）")
        return 0
    _register_activation(licensing, args.code)
    return 0


def cmd_export(args) -> int:
    """数据导出（PIPL）：用例 + 索引 + 报告与执行记录；--media 含截图。"""
    import zipfile
    out = Path(args.out)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for pattern in ("cases/*.json", "artifacts/page_index/*.json", "artifacts/imports/*/import.json"):
            for f in Path(".").glob(pattern):
                zf.write(f, f.as_posix())
                count += 1
        for run_json in Path("artifacts").glob("*/run.json"):
            zf.write(run_json, run_json.as_posix())
            count += 1
            report = run_json.parent / "report.html"
            if report.exists():
                zf.write(report, report.as_posix())
                count += 1
        if args.media:
            for shot in Path("artifacts").glob("*/screenshots/*.png"):
                zf.write(shot, shot.as_posix())
                count += 1
    print(f"已导出 {count} 个文件 → {out.resolve()}（数据可携带权，PIPL）")
    return 0


def _print_server_hint(inv, state: dict, stream=None) -> None:
    """把凭证**用起来** —— 否则「免费档可以有服务端通道」在体验上没闭环。

    拿到 token 却不知道往哪放，等于这个通道不存在。所以这里给出
    **可直接复制的 `.env` 片段**。

    🔴 **唯一的调用方是 `mat token`**（2026-10-14 起）。`mat invite` 不再打印凭证 ——
    凭证只有一个出口，这里就是那个出口（见 `cmd_invite` 里的说明）。

    ⚠️ 只打印片段，**不自动改用户的 `.env`**：改配置是显式动作，不该由一条命令代劳
    （同族纪律：不许"顺手帮用户改配置"）。
    ⚠️ 已经在用服务端（`GEN_PROMPT_MODE=server`）就不再唠叨。
    ⚠️ `stream` 让 `mat token` 把这段挤到 **stderr** —— 那条命令的 stdout 只许有 token
    （否则 `mat token --new | pbcopy` 会把说明文字一起复制走，实测踩过）。
    ⚠️ 默认值必须写 `None`、而不是 `sys.stdout`：**默认参数在函数定义时求值**，
    会把"模块导入那一刻的 stdout"绑死；而 pytest 的 `capsys` 会替换 `sys.stdout`
    并在用例结束后关掉它 ⇒ `file=<那个旧对象>` 抛
    `ValueError: I/O operation on closed file`（实测踩过：一次改动让 7 条邀请测试变红）。
    """
    stream = stream if stream is not None else sys.stdout
    token = str(state.get("token") or "")
    if not token or os.environ.get("GEN_PROMPT_MODE", "").strip().lower() == "server":
        return
    print("\n想用官方代理（不必自备模型 Key）？把下面三行加进工作区 .env：", file=stream)
    print(f"  GEN_BASE_URL={inv.cloud_url()}", file=stream)
    print(f"  GEN_API_KEY={token}", file=stream)
    print("  GEN_PROMPT_MODE=server", file=stream)


def _license_status_lines() -> list[str]:
    """`mat status` 里的 Pro 服务端授权行（T17）。**只读本地，不联网。**"""
    from . import licensing

    if not licensing.read_server_token():
        return []
    try:
        meta = json.loads(licensing.server_token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if meta.get("machines"):
        return [f"Pro 服务端授权: 已登记，本码绑定 {meta['machines']}/{meta.get('max_machines', '?')} 台机器"
                "（`mat activate --machines` 查看，`--unbind` 解绑）"]
    return ["Pro 服务端授权: 已登记（`mat activate --machines` 查看绑定）"]


def _invite_status_lines() -> list[str]:
    """`mat status` 里的邀请行。**只读本地缓存，绝不联网**。

    为什么不在 status 里拉台账：`mat status` 要快、要能离线用；而且「只有 `mat invite*`
    会出网」是一条**可验证**的纪律（§6.5）—— 破一次就再也说不清了。
    台账快照由 `mat invite` 写进本地状态。
    """
    from . import invite as inv

    state = inv.load_state()
    code = state.get("invite_code")
    if not code:
        return []
    pretty = f"{str(code)[:4]}-{str(code)[4:]}"
    cached = state.get("ledger_cache") or {}
    synced = state.get("ledger_synced_at")
    if not cached or not synced:
        return [f"邀请: 邀请码 {pretty}（台账还没同步过，运行 `mat invite` 查看）"]
    return [
        f"邀请: 已邀请 {cached.get('invited', 0)} 人 · 已到账 {cached.get('qualified', 0)} 人"
        f" · 待生效 {cached.get('pending', 0)} 人（同步于 {synced}）",
        f"      邀请码 {pretty} · 本月计酬 {cached.get('credited_this_month', 0)}"
        f"/{cached.get('monthly_cap', 0)}",
    ]


def _notify_invite_after_run() -> None:
    """一轮执行正常结束后，告诉邀请方「被邀请人跑通了」（里程碑）。

    ⚠️ **没接受过邀请时一个请求都不发** —— `notify_first_run` 先看本地状态，
    没有 `accepted` 就直接返回。跑测试的命令不该偷偷出网。
    ⚠️ 失败一律吞掉：**邀请是旁路功能，跑测试绝不能因为它而失败**。
    """
    try:
        from . import invite

        invite.notify_first_run()
    except Exception:            # 任何异常都不影响本次执行结果
        pass


def _print_ledger(led: dict, synced_at: str) -> None:
    """打印邀请台账。**`待生效` 与 `超额未计酬` 都必须显示**。"""
    kind = {"server_gen": "服务端生成", "pro_days": "Pro 时长"}.get(
        str(led.get("reward_kind")), str(led.get("reward_kind")))
    unit = "次" if led.get("reward_kind") == "server_gen" else "天"
    print(f"\n邀请台账（同步于 {synced_at}）")
    print(f"  已邀请        {led.get('invited', 0)} 人")
    print(f"  已到账        {led.get('qualified', 0)} 人"
          f"（本月 {led.get('credited_this_month', 0)}/{led.get('monthly_cap', 0)}）")
    pending = led.get("pending", 0)
    if pending:
        print(f"  待生效        {pending} 人  ← 对方还没跑通第一轮")
    capped = led.get("capped", 0)
    if capped:
        print(f"  ⚠️ 超额未计酬  {capped} 人  ← 已达标，但本月已达上限，不再计酬")
    print(f"  每次奖励      {kind} {led.get('reward_amount', 0):g} {unit}")


def _report_pro_reward(claim: dict | None, led: dict) -> None:
    """把邀请挣来的 Pro 时长落到本机。`claim` 由 `invite.sync_ledger` 给。

    ⚠️ 只在**比本机现有有效期更晚**时才激活（判断在 `invite.claim_pro_reward` 里）：
    服务端签的是「现在 + 累计天数」，用户自己买的年付可能更长，直接覆盖会把有效期**改短**。
    """
    if claim and claim.get("activated"):
        until = datetime.fromtimestamp(claim["expires_at"]).strftime("%Y-%m-%d")
        print(f"🎁 已自动激活邀请奖励：Pro 有效期至 {until}")
    elif claim and claim.get("reason"):
        print(f"（邀请奖励未改动本机授权：{claim['reason']}）")
    elif (led.get("pro_code") or {}).get("status") == "pending_sign":
        print("🎁 邀请奖励已记账（Pro 时长），但服务端还没签发激活码 ——"
              "等运营配好签名密钥后重跑本命令即可领取。")


def cmd_invite(args) -> int:
    """`mat invite`：邀请码 / 接受邀请 / 确认跑通。

    ⚠️ 这是**唯一**会向轻云发起"注册"请求的命令（可验证纪律）。
    别把注册挪到启动路径上 —— 那等于静默给用户建账号，与「数据不出内网」正面冲突。
    """
    from . import invite as inv

    if getattr(args, "accept", None):
        try:
            out = inv.accept(args.accept)
        except inv.InviteError as exc:
            print(f"接受邀请失败：{exc}", file=sys.stderr)
            return 1
        print(f"已接受邀请（{out.get('status', 'pending')}）。{out.get('note', '')}")
        print("你跑通第一轮后，邀请人将获得额度（会自动通知，无需你再操作）。")
        return 0

    if getattr(args, "confirm", False):
        try:
            out = inv.qualify()
        except inv.InviteError as exc:
            print(f"确认失败：{exc}", file=sys.stderr)
            return 1
        if out.get("qualified"):
            print("已确认跑通首轮，邀请人已计酬。")
        else:
            print("这次没有计酬（已经计过，或你没有接受过别人的邀请）。")
        return 0

    state = inv.load_state()
    if not state.get("invite_code"):
        # 🔴 首次注册**必须让用户有感知**：出站注册与「数据不出内网」
        # 正面冲突，被发现一次的代价远大于问一次的摩擦。
        print("首次使用邀请功能，将向 MAT 官方服务注册一个**匿名 ID**：")
        print("  · 只上传一个「公钥哈希」，不含账号 / 邮箱 / 手机 / 设备标识")
        print(f"  · 服务地址：{inv.cloud_url()}")
        print("  · 只有这一步需要联网；不邀请就不用注册，本地功能完全不受影响")
        if not getattr(args, "yes", False):
            if not sys.stdin.isatty():
                print("拒绝在非交互环境下静默注册。确认上面说明后加 --yes 重跑。", file=sys.stderr)
                return 1
            try:
                answer = input("继续？[Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer not in ("", "y", "yes"):
                print("已取消（本地其余功能不受影响）。")
                return 1
        try:
            state = inv.register()
        except inv.InviteError as exc:
            print(f"注册失败：{exc}", file=sys.stderr)
            return 1

    code = str(state["invite_code"])
    pretty = f"{code[:4]}-{code[4:]}"
    print(f"\n邀请码：{pretty}")
    print(f"把邀请码发给同事；他装好 MAT 后执行：mat invite --accept {pretty}")
    # 🔴 **不打印凭证**（2026-10-14 发起人定）：凭证只有一个出口 —— `mat token`。
    # 这里以前会顺带打印 `.env` 三行（含 GEN_API_KEY），那时候"邀请"和"取凭证"混在一条
    # 命令里：拿到邀请码的人会以为那就是凭证，而"凭证去哪拿"这件事也就没了门槛。
    # 注意 `state` 里仍然**存着** token（`inv.register()` 落的盘）—— 只是不在这里说。
    #
    # 但"完全不说"会让跑过 `mat invite` 的人在产品内**没有任何线索**，所以留一句
    # **不点名命令**的指路（发起人 2026-10-14 选的方案）：只说去哪个公众渠道拿，
    # 命令名依旧只在那边给 —— 门槛不破，路也不断。
    if os.environ.get("GEN_PROMPT_MODE", "").strip().lower() != "server":
        print("想用官方代理（不必自备模型 Key）？关注公众号「Mobile AI Test」免费获取凭证。")

    try:
        out = inv.sync_ledger()
    except inv.InviteError as exc:
        # ⚠️ 读不到台账**不许显示 0**（§6.4）—— `0` 会被读成「我的邀请全丢了」。
        print(f"\n台账暂时读不到：{exc}")
        last = state.get("ledger_synced_at")
        print("⚠️ 这不等于你没有邀请，只是这次没连上。"
              + (f"上次同步：{last}" if last else ""))
        return 0

    led = out["ledger"]
    _print_ledger(led, str(inv.load_state().get("ledger_synced_at", "")))
    _report_pro_reward(out.get("claim"), led)
    return 0


def cmd_token(args) -> int:
    """`mat token`：查看本机的服务端凭证；加 `--new` 轮换一张。

    为什么单独一条命令（2026-10-14 发起人要求）：`mat invite` 是**邀请**语义
    （拿邀请码、看谁跑通了），而"拿一张能用于官方代理的 token"是**独立的日常动作**。
    混在一起会让 `mat invite` 既拉台账又吐凭证，职责不清，也没法"只想取 token"。

    ⚠️ 默认**只读本地**（`~/.mat/invite.json`）、**不联网、不重签**：
    轮换会让正在用这张 token 的 `.env` 立刻失效 —— 不能因为"看一眼"就发生。
    要轮换必须显式 `--new`（那时服务端会吊销该身份此前所有凭证，只留最新一张）。
    """
    from . import invite as inv

    state = inv.load_state()
    token = str(state.get("token") or "")

    if not getattr(args, "new", False):
        if not token:
            print("本机还没有凭证 —— 跑 `mat token --new` 领一张"
                  "（会向官方服务匿名注册，需联网）。", file=sys.stderr)
            return 1
        print(token)
        _print_server_hint(inv, state, stream=sys.stderr)
        return 0

    if not state.get("invite_code"):
        # 与 `mat invite` 同一套知情流程（首次注册必须让用户有感知）
        # ⚠️ 全程走 **stderr**：这条命令的 stdout **只许有 token**（`mat token --new | pbcopy`）。
        print("首次使用将向 MAT 官方服务注册一个**匿名 ID**：", file=sys.stderr)
        print("  · 只上传一个「公钥哈希」，不含账号 / 邮箱 / 手机 / 设备标识", file=sys.stderr)
        print(f"  · 服务地址：{inv.cloud_url()}", file=sys.stderr)
        print("  · 只有这一步需要联网；不用官方代理就不必注册", file=sys.stderr)
        if not getattr(args, "yes", False):
            if not sys.stdin.isatty():
                print("拒绝在非交互环境下静默注册。确认上面说明后加 --yes 重跑。",
                      file=sys.stderr)
                return 1
            sys.stderr.write("继续？[Y/n] ")
            sys.stderr.flush()
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer not in ("", "y", "yes"):
                print("已取消（本地其余功能不受影响）。", file=sys.stderr)
                return 1

    try:
        state = inv.register()
    except inv.InviteError as exc:
        print(f"领取失败：{exc}", file=sys.stderr)
        return 1

    print(str(state.get("token") or ""))
    print("（已重签：该身份**此前所有凭证已作废**，只留这一张）", file=sys.stderr)
    _print_server_hint(inv, state, stream=sys.stderr)
    return 0


def _cmd_token_hidden(argv: list[str]) -> int:
    """`mat token` 的**隐藏入口** —— 故意不走 argparse。

    为什么要绕开 argparse（2026-10-14 发起人定「轻门槛」）：`add_parser` 的
    `help=argparse.SUPPRESS` **藏不住** —— 只去掉 help 文案，子命令名**仍列在 usage 行**
    （`{init,…,token,…}`），实测还会打印 `==SUPPRESS==` 字面量。要真的不出现在
    `mat --help` 里，只能在 `main()` 进入 argparse **之前**把它拦下来。

    门槛的含义：**知道命令名才能用**。获取方式只在公众号回复关键词时给，
    官网 / 使用手册 / 发布说明都不提。
    ⚠️ 这是「**隐晦**」而不是「限制」：`POST /invite/register` 端点本身仍是**开放**的，
    会读文档 / 抓包的人能自己发请求拿到凭证。它挡的是"随手试试"的人，不是有心的。
    ⚠️ 这里的参数是**手工解析**的 —— 加参数时别忘了同步改这段（没有 argparse 兜底）。
    """
    args = argparse.Namespace(new=False, yes=False)
    for a in argv:
        if a == "--new":
            args.new = True
        elif a == "--yes":
            args.yes = True
        elif a in ("-h", "--help"):
            print("用法: mat token [--new] [--yes]\n"
                  "\n"
                  "查看本机的服务端凭证（官方代理用）。\n"
                  "\n"
                  "  --new  重签一张（该身份此前所有凭证作废，只留最新一张）\n"
                  "  --yes  跳过首次注册的确认提示（脚本 / CI 用）")
            return 0
        else:
            print(f"未知参数：{a}（可用：--new / --yes）", file=sys.stderr)
            return 2
    return cmd_token(args)


def _server_usage() -> dict | None:
    """server 模式：从代理拉取服务端权威用量（拉不到返回 None，回退本地计数）。"""
    base = os.environ.get("GEN_BASE_URL", "").rstrip("/")
    key = os.environ.get("GEN_API_KEY", "")
    if not base or not key:
        return None
    try:
        import requests

        resp = requests.get(f"{base}/usage",
                            headers={"Authorization": f"Bearer {key}"}, timeout=8)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {"month": data.get("month"), "plan": data.get("plan"),
                "gen": data.get("gen", {}), "vision": data.get("vision", {})}
    except Exception:
        return None


def cmd_status(_args) -> int:
    usage = UsageStore().snapshot()
    from . import promptstore

    server = promptstore.is_server()
    remote = _server_usage() if server else None
    if remote:
        gen = remote["gen"]
        print(f"MAT v{__version__}  套餐: {remote['plan']}（服务端）")
        print(f"NL 生成（{remote['month']}）: {gen.get('used', 0):g}/{gen.get('limit', '?'):g}"
              " · 本地执行不限量")
    else:
        limit_text = usage["gen_limit"] if usage["gen_limit"] is not None else "不限"
        print(f"MAT v{__version__}  套餐: {usage['plan']}")
        print(f"NL 生成（{usage['month']}）: {usage['gen_count']}/{limit_text} · 本地执行不限量")
    key_ok = bool(os.environ.get("GEN_API_KEY"))
    endpoint = "已配置" if key_ok else "未配置（gen 不可用，本地执行不受影响）"
    print(f"AI 端点: {endpoint}" + ("  [轻云代理，prompt 服务端组装]" if server else ""))
    vl = os.environ.get("GEN_VL_MODEL")
    print("视觉兜底: " + vision_status_text(server=server, key_ok=key_ok, vl=vl))
    vision_limit_text = usage["vision_limit"] if usage["vision_limit"] is not None else "不限"
    print(f"视觉兜底用量（{usage['month']}）: {usage['vision_count']}/{vision_limit_text} · 同屏缓存命中不计费")
    for line in _license_status_lines():
        print(line)
    for line in _invite_status_lines():
        print(line)
    if server:
        print("生成 prompt: 由轻云代理下发（本地无需 prompt 文件，闭源 prompt 不下发）")
    else:
        prompt = promptstore.resolve("gen")
        print(f"生成 prompt: {prompt.as_posix() if prompt else '未找到（运行 mat init 播种 prompts/）'}")
    creds = CredentialStore().names()
    print(f"凭据: {len(creds)} 条" + (f"（{', '.join(creds)}；用例中以 ${{cred.名称}} 引用）" if creds else ""))
    from .notify import configured
    if configured():
        print("结果通知: 已配置（MAT_WEBHOOK_URL）")
    else:
        print("结果通知: 未配置（可选）——在运行目录 .env 加 MAT_WEBHOOK_URL"
              "（飞书/钉钉机器人地址，见手册 §4.9）")
    devices = list_adb_devices()
    print(f"设备: {len(devices)} 台在线")
    indexes = sorted(Path("artifacts/page_index").glob("*.json")) if Path("artifacts/page_index").exists() else []
    if indexes:
        from .page_index import load_index
        parts = []
        for f in indexes:
            graph = load_index(f) or {}
            pages = graph.get("pages", [])
            described = sum(1 for p in pages if p.get("summary_source") == "ai")
            suffix = f"/已描述{described}" if described else ""
            parts.append(f"{f.stem}({len(pages)}页/{len(graph.get('edges', []))}边{suffix})")
        print(f"页面图谱: {'、'.join(parts)}（explore 自动沉淀，describe 补描述）")
    else:
        print("页面图谱: 无（mat index <包名> 构建，gen/convert 防幻觉）")
    print("下一步: mat console   # 打开本地控制台")
    return 0


def cmd_cred(args) -> int:
    store = CredentialStore()
    if args.cred_cmd == "set":
        import getpass
        value = getpass.getpass(f"输入 {args.name} 的值（输入不回显）: ")
        store.set(args.name, value)
        print(f"已保存凭据 {args.name}；用例中以 ${{cred.{args.name}}} 引用")
        return 0
    if args.cred_cmd == "list":
        names = store.names()
        print("\n".join(names) if names else "（无凭据）")
        return 0
    if args.cred_cmd == "del":
        print(f"已删除 {args.name}" if store.delete(args.name) else f"凭据 {args.name} 不存在")
        return 0
    return 0


def cmd_heal(args) -> int:
    if args.heal_cmd == "list":
        proposals = collect_proposals(Path(args.run_dir))
        if not proposals:
            print("该执行没有自愈提案")
            return 0
        for p in proposals:
            print(f"{p['step']}: {p['original']} → {p['match']}")
        print(f"\n回写: mat heal apply <用例.json> --run {args.run_dir}")
        return 0
    case = TestCase.model_validate_json(Path(args.case).read_text(encoding="utf-8"))
    proposals = collect_proposals(Path(args.run))
    new_case, notes = apply_heals(case, proposals)
    applied = [n for n in notes if "跳过" not in n]
    if not applied:
        print("没有可回写的提案（原定位与提案不匹配，或已回写过）")
        return 1
    for note in notes:
        print(("回写: " if "跳过" not in note else "跳过: ") + note)
    out = Path(args.out) if args.out else Path(args.case).with_name(Path(args.case).stem + "-healed.json")
    out.write_text(dump_dsl(new_case), encoding="utf-8")
    print(f"已写出 {out}（不覆盖原用例；真机复核后自行替换，动作已留审计说明）")
    return 0


def cmd_skill(args) -> int:
    """`mat skill`：查看随包 skill 的位置 / 装进 AI 助手的 skill 目录。

    存在的理由：skill 此前只在仓库根目录，靠公开仓分发 ⇒ 只 pip 安装的用户取不到；
    本命令让它跟 `mat-mcp` 一样成为**装完就有**的能力。
    """
    from . import skill as _skill

    if args.skill_cmd == "path":
        directory = _skill.bundled_skill_dir()
        if not directory.is_dir():
            print(f"包内找不到 skill：{directory}")
            print("安装包可能不完整——重装：pip install --force-reinstall mat-uitest")
            return 1
        print(f"随包 skill 目录: {directory}")
        for path in _skill.bundled_files():
            print(f"  {path.name}")
        print("装进 AI 助手：mat skill install（或把上面的目录整个复制到助手的 skill 目录）")
        return 0

    try:
        root = _skill.resolve_target(getattr(args, "target", None), getattr(args, "dir", None))
        result = _skill.install(root, force=getattr(args, "force", False))
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc))
        return 1

    status, dest = result["status"], result["target"]
    if status == "up-to-date":
        print(f"已是最新，无需操作：{dest}")
        return 0
    if status == "differs":
        print(f"目标已存在且内容不同：{dest}")
        print(f"  差异文件：{'、'.join(result['differing'])}")
        print("  为免覆盖你自己改过的内容，本次**没有安装**。确认覆盖请加 --force 重跑。")
        return 1
    print(f"{'已覆盖更新' if result.get('overwritten') else '已安装'}：{dest}"
          f"（{len(result['files'])} 个文件）")
    print("  用法：在 AI 编码助手里说「用 mobileapp-testing 技能测一下 <你的 App>」；"
          "新会话才会加载新装的 skill。")
    return 0


def _resolve_console_root(cwd: Path, override: str | None) -> tuple[Path, str]:
    """console 工作区解析（防 artifacts 散落）：
    ① --root 显式指定；② 当前目录已是工作区（含 cases/ 或 artifacts/）→ 用它；
    ③ 否则回退 ~/.insta/workspace（自动创建）——而不是在启动目录悄悄散落一套产物。"""
    if override:
        root = Path(override).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root, f"工作区(--root): {root}"
    if (cwd / "cases").exists() or (cwd / "artifacts").exists():
        return cwd, f"工作区: {cwd}"
    root = Path.home() / ".mat" / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root, (f"工作区: {root}（当前目录 {cwd} 不是工作区——cases/artifacts 不放这里；"
                  f"如需在此建工作区请先 mat init）")


def cmd_console(args) -> int:
    from .web_server import lan_urls, serve

    root, root_note = _resolve_console_root(Path.cwd(), getattr(args, "root", None))
    # 工作区 .env 才是 GEN_*/MAT_WEBHOOK_URL 的真实来源（--root 或回退目录时 cwd/.env
    # 根本不存在）——不加载它，环境检测永远「未配置」，配置台「打开配置→改→重新检测」
    # 的闭环也是假的。main() 已加载的进程环境优先（不覆盖），语义与 MCP/CLI 一致。
    _load_env_file(root / ".env")
    print(root_note)
    # 团队成员数：打开控制台时顺手同步一次服务端配额（best-effort —— 断网、服务端没起都不该挡住启动；
    # 拉不到就继续用本地缓存里各批的到期时间算，见 seat_quota）。
    try:
        from . import seat_quota
        from .cloudapi import CloudError

        seat_quota.sync_from_server()
    except (CloudError, OSError):
        pass
    url = f"http://127.0.0.1:{args.port}"
    print(f"控制台: {url}  （Ctrl+C 退出）")
    if getattr(args, "share", False):
        # T55 P0：局域网分享——报告/执行页可从同网段浏览器直开，数据零出域。
        # 团队协作 P1（发起人 2026-10-12）：口令鉴权——此前无鉴权，同网段任何人可删改用例/烧额度。
        import secrets
        password = getattr(args, "password", None) or secrets.token_hex(3)  # 6 hex，短到能口头转述
        print("⚠️ 局域网分享开启：同网段内需访问口令（含运行/删改用例权限），仅限可信内网。")
        print(f"🔑 访问口令: {password}   （同事首次打开链接时输入一次即可）")
        urls = lan_urls(args.port)
        if urls:
            print("内网分享链接（发给同一 Wi-Fi/VPN 下的同事，浏览器直接打开）:")
            for u in urls:
                print(f"  {u}")
        else:
            print("[!] 未枚举到局域网地址——检查网络连接后重启。")
    else:
        print("（仅本机监听；要分享给同事用 `mat console --share`）")
        password = None
    print("下一步: 手机插上 → 用例工作台运行 → 执行页看事件流 → 报告页看结果")
    if not args.no_open:
        import threading
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        serve(args.port, root=root, host="0.0.0.0" if getattr(args, "share", False) else "127.0.0.1",
              auth_key=password)
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


def cmd_mcp(args) -> int:
    """MCP 服务器：stdio 长驻，把 MAT 能力暴露给 AI 编码工具。"""
    from .mcp_server import main as mcp_main

    mcp_main(args.root)
    return 0


def _load_env_file(path: Path | None = None) -> None:
    """加载工作区 .env（GEN_* 等）；已设置的环境变量不覆盖。"""
    env_path = path or Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if " #" in value:  # 行内注释
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> None:
    _load_env_file()
    argv = list(sys.argv[1:] if argv is None else argv)
    # 隐藏命令**优先拦截**（在 argparse 之前，见 `_cmd_token_hidden` 的说明）
    if argv and argv[0] == "token":
        sys.exit(_cmd_token_hidden(argv[1:]))
    parser = argparse.ArgumentParser(prog="insta-engine", description=f"MAT 执行引擎 v{__version__}")
    parser.add_argument("--version", action="version", version=f"mat {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化工作区（cases/ artifacts/ + 示例用例）").set_defaults(func=cmd_init)
    sub.add_parser("devices", help="列出在线设备").set_defaults(func=cmd_devices)
    sub.add_parser("status", help="套餐/配额/凭据/设备一览").set_defaults(func=cmd_status)
    seat_p = sub.add_parser("seat", help="团队成员：本机标识 / 同步配额 / 查看现状")
    seat_sub = seat_p.add_subparsers(dest="seat_action")
    seat_sub.add_parser("id", help="打印本机标识（发给运营，用于给这台机器增加成员名额）")
    seat_sub.add_parser("sync", help="从服务端拉取本机成员配额（需联网）")
    seat_sub.add_parser("status", help="显示成员上限与各批到期（缺省动作）")
    seat_p.set_defaults(func=cmd_seat)
    console_p = sub.add_parser("console", help="打开本地控制台（http://127.0.0.1:7420，仅本机监听）")
    console_p.add_argument("--share", action="store_true",
                           help="局域网分享：监听所有网卡并打印内网链接（需访问口令，仅限可信内网）")
    console_p.add_argument("--password", default=None, metavar="口令",
                           help="--share 的访问口令；缺省自动生成并打印（非 --share 时忽略）")

    console = sub.choices["console"]
    console.add_argument("--port", type=int, default=7420)
    console.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    console.add_argument("--root", default=None,
                         help="工作区根目录（缺省规则见 --help 顶部说明：当前目录是工作区则用它，否则 ~/.mat/workspace）")
    console.set_defaults(func=cmd_console)

    mcp_p = sub.add_parser("mcp", help="启动 MCP 服务器（stdio，供 Cursor/Codex 等 AI 工具接入；需 mat-uitest[mcp]）")
    mcp_p.add_argument("--root", default=None, help="工作区根目录（缺省规则同 mat console）")
    mcp_p.set_defaults(func=cmd_mcp)

    cred = sub.add_parser("cred", help="凭据管理（用例中以 ${cred.名称} 引用）")
    cred_sub = cred.add_subparsers(dest="cred_cmd", required=True)
    cred_set = cred_sub.add_parser("set", help="保存凭据（输入不回显）")
    cred_set.add_argument("name")
    cred_set.set_defaults(func=cmd_cred)
    cred_sub.add_parser("list", help="列出凭据名（不显示值）").set_defaults(func=cmd_cred)
    cred_del = cred_sub.add_parser("del", help="删除凭据")
    cred_del.add_argument("name")
    cred_del.set_defaults(func=cmd_cred)

    exp = sub.add_parser("explore", help="探索测试（规则驱动，发现崩溃/白屏；时长与步数双上限）")
    exp.add_argument("package", help="被测 App 包名")
    exp.add_argument("--minutes", type=float, default=2.0, help="时长预算（分钟，默认 2）")
    exp.add_argument("--steps", type=int, default=30, help="步数预算（默认 30）")
    exp.add_argument("--max-depth", type=int, default=3, help="页面层级上限（L0 起，默认 3）")
    exp.add_argument("--serial", help="设备 serial")
    exp.add_argument("--platform", choices=("android", "ios"), default="android",
                     help="设备平台（iOS=模拟器 MVP；explore 的 iOS 适配在 P3）")
    exp.set_defaults(func=cmd_explore)

    doc = sub.add_parser("doctor", help="环境自检（adb/驱动/AI 配置/设备）")
    doc.set_defaults(func=cmd_doctor)
    act = sub.add_parser("activate", help="激活 Pro（用户）；--machines/--unbind 管理机器绑定")
    act.add_argument("code", nargs="?", help="激活码（用 --machines / --unbind 时可省略）")
    act.add_argument("--machines", action="store_true", help="查看本激活码已绑定的机器（T17）")
    act.add_argument("--unbind", metavar="机器标识", help="解绑一台机器（换机/重装场景）")
    act.set_defaults(func=cmd_activate)
    inv_p = sub.add_parser("invite", help="邀请同事用 MAT，分享人得额度（需联网；仅本命令会注册匿名 ID）")
    inv_p.add_argument("--accept", metavar="邀请码", help="接受别人的邀请（对方将获得额度）")
    inv_p.add_argument("--confirm", action="store_true",
                       help="手动确认「我已跑通第一轮」（自动通知没生效时的兜底）")
    inv_p.add_argument("--yes", action="store_true", help="跳过首次注册的确认提示（脚本用）")
    inv_p.set_defaults(func=cmd_invite)
    # ⚠️ `token` **故意不在这里注册** —— 它不进 `mat --help`。
    # 为什么不注册：`add_parser(help=argparse.SUPPRESS)` **藏不住**，子命令名仍会出现在
    # usage 行（`{init,…,token,…}`）、还打印 `==SUPPRESS==` 字面量（实测）。
    # 要真"看不见"，只能在进入 argparse 之前拦下来，见 `_cmd_token_hidden`。
    exp_out = sub.add_parser("export", help="导出全部数据（PIPL 数据可携带权）")
    exp_out.add_argument("-o", "--out", default="mat-export.zip")
    exp_out.add_argument("--media", action="store_true", help="包含截图（体积大）")
    exp_out.set_defaults(func=cmd_export)

    idx = sub.add_parser("index", help="真机爬取构建页面索引（控件清单，gen/convert 防幻觉底座）")
    idx.add_argument("package", help="被测 App 包名")
    idx.add_argument("--serial", help="设备 serial")
    idx.add_argument("--platform", choices=("android", "ios"), default="android",
                     help="设备平台（iOS 适配在 P3）")
    idx.add_argument("--refresh", action="store_true", help="强制重新爬取")
    idx.add_argument("--steps", type=int, default=30, help="爬取步数预算（默认 30）")
    idx.add_argument("--pages", type=int, default=15, help="页面数预算（默认 15）")
    idx.set_defaults(func=cmd_index)

    desc = sub.add_parser("describe", help="给图谱页面补标题+功能描述（AI，可选；计 1 次生成额度）")
    desc.add_argument("package", help="被测 App 包名")
    desc.add_argument("--limit", type=int, default=None,
                      help="单次描述的页面数上限（缺省=全部剩余页；一次调用描述整批，计 1 次额度）")
    desc.add_argument("--refresh", action="store_true", help="重算已有 AI 描述（人工标题不覆盖）")
    desc.set_defaults(func=cmd_describe)

    rev = sub.add_parser("review", help="人工复核页面标题（生成复核页 / 应用结论 / 重放）")
    rev.add_argument("package", help="被测 App 包名")
    rev.add_argument("--apply", metavar="FILE", default="",
                     help="应用复核页导出的 JSON（落盘到 review/<包名>.json 并写回图谱）")
    rev.add_argument("--replay", action="store_true",
                     help="从 review/<包名>.json 重放（图谱被重跑覆盖后用它恢复人工标题）")
    rev.set_defaults(func=cmd_review)

    cal = sub.add_parser("calibrate", help="报告校准（改判失败归类，并把结论落成规则）")
    cal.add_argument("run_dir", nargs="?", default="", help="执行产物目录（含 run.json），如 artifacts/case_xxx")
    cal.add_argument("--apply", metavar="FILE", default="",
                     help="应用校准页导出的 JSON（改 run.json + 重渲染报告 + 落成规则）")
    cal.add_argument("--show-rules", action="store_true", help="列出累积的校准规则（不读产物目录）")
    cal.set_defaults(func=cmd_calibrate)

    diag = sub.add_parser("diagnose", help="LLM 失败归因（失败步骤 → 四分类+证据+建议；无 Key 自动回退规则结果）")
    diag.add_argument("run_dir", help="执行产物目录（含 run.json），如 artifacts/case_xxx")
    diag.set_defaults(func=cmd_diagnose)

    arr = sub.add_parser("arrival", help="试跑验证图谱到达路径（重放 N 轮，通过 ≥N-1 记 stable）")
    arr.add_argument("package", help="被测 App 包名")
    arr.add_argument("--trials", type=int, default=3, help="每条路径试跑轮数（默认 3，最小 2）")
    arr.add_argument("--max-paths", type=int, default=8, help="本次最多验证的路径数（默认 8，浅链优先）")
    arr.add_argument("--serial", help="设备 serial")
    arr.add_argument("--platform", choices=("android", "ios"), default="android",
                     help="设备平台（iOS 适配在 P3）")
    arr.set_defaults(func=cmd_arrival)

    imp = sub.add_parser("import", help="导入手工用例（Excel/CSV/Markdown/文本 → DSL 草稿）")
    imp.add_argument("file", help="用例文件（.csv/.xlsx/.md/.txt）")
    imp.add_argument("--package", required=True, help="被测 App 包名")
    imp.add_argument("--template", default="auto", help="映射模板 JSON；auto=表头启发式（失败时提示 AI 映射/手写模板）")
    imp.add_argument("--limit", type=int, help="仅转换前 N 条（先小批试跑）")
    imp.add_argument("--no-expand-preconditions", dest="expand_preconditions",
                     action="store_false",
                     help="不把「可置备的页面状态」前置条件展开成导航步骤（默认展开；"
                          "展开会多用一次 gen 额度/条，见 preconditions.py 的四分类）")
    imp.set_defaults(func=cmd_import, expand_preconditions=True)

    gen = sub.add_parser("gen", help="自然语言生成用例（需 GEN_* 环境变量，见 docs/13-产品使用手册.md）")
    gen.add_argument("nl", help="自然语言测试场景描述")
    gen.add_argument("--package", required=True, help="被测 App 包名")
    gen.add_argument("-o", "--out", default="case.json")
    gen.add_argument("--calibrate", action="store_true", help="真机 dry-run 校准（≤3 轮失败回喂修正）")
    gen.add_argument("--max-rounds", type=int, default=3)
    gen.add_argument("--serial", help="校准用设备 serial")
    gen.add_argument("--platform", choices=("android", "ios"), default="android",
                     help="校准设备平台（仅 --calibrate 用；iOS=WDA 模拟器 MVP）")
    gen.set_defaults(func=cmd_gen)

    run = sub.add_parser("run", help="执行用例/套件 JSON")
    run.add_argument("case", help="用例/套件 JSON 文件，或用例目录（配合 --app 批量）")
    run.add_argument("--app", help="按包名过滤/校验：单用例校验 target.package 一致，目录则批量跑该 App 全部用例")
    run.add_argument("--serial", help="设备 serial（缺省取唯一在线设备）")
    run.add_argument("--platform", choices=("android", "ios"), default="android",
                     help="设备平台：android（默认）或 ios（模拟器 MVP，需 WDA 在 localhost:8100）")
    run.add_argument("--record", action="store_true",
                     help="执行录屏：全程录制，通过自动删除、失败保留进 artifacts/video/（报告内嵌播放器）")
    run.add_argument("--artifacts", help="产物目录（默认 artifacts/<文件名>）")
    run.add_argument("--junit-xml", help="输出 JUnit XML（Jenkins/GitLab/Actions 通用）")
    run.add_argument("--json", action="store_true", help="末尾追加机器可读 JSON 摘要（CI 用）")
    run.add_argument("--dataset", help="数据驱动选组：all（默认）或逗号分隔的组名/序号（如 alice 或 1,3）")
    run.add_argument("--allow-unmet-preconditions", action="store_true",
                     help="放行「前置条件 MAT 无法置备」的用例（默认拒跑，见 preconditions.py）")
    run.set_defaults(func=cmd_run)

    heal = sub.add_parser("heal", help="自愈提案查看与回写（回写不覆盖原用例）")
    heal_sub = heal.add_subparsers(dest="heal_cmd", required=True)
    heal_list = heal_sub.add_parser("list", help="查看某次执行的自愈提案")
    heal_list.add_argument("run_dir", help="执行产物目录（如 artifacts/case_t1）")
    heal_list.set_defaults(func=cmd_heal)
    heal_apply = heal_sub.add_parser("apply", help="把提案回写为 -healed.json 新用例")
    heal_apply.add_argument("case", help="原用例 JSON")
    heal_apply.add_argument("--run", required=True, help="执行产物目录")
    heal_apply.add_argument("-o", "--out", help="输出文件（默认 <用例名>-healed.json）")
    heal_apply.set_defaults(func=cmd_heal)

    skill_p = sub.add_parser(
        "skill", help="AI 编码助手技能包（mobileapp-testing）：查看路径 / 装进助手")
    skill_sub = skill_p.add_subparsers(dest="skill_cmd", required=True)
    skill_sub.add_parser("path", help="打印随包 skill 的位置（供手动复制）").set_defaults(
        func=cmd_skill)
    skill_install = skill_sub.add_parser("install", help="安装到 AI 助手的 skill 目录")
    skill_install.add_argument("--target", choices=sorted(KNOWN_TARGETS), default="claude",
                               help="已知落点：claude=~/.claude/skills（默认）、"
                                    "workbuddy=~/.workbuddy/skills")
    skill_install.add_argument("--dir", default=None,
                               help="自定义安装根目录（优先于 --target）")
    skill_install.add_argument("--force", action="store_true",
                               help="目标已存在且内容不同时覆盖（默认不覆盖，保护你的改动）")
    skill_install.set_defaults(func=cmd_skill)

    # mat suite —— 套件（测试项目）管理。子命令实现在 suite_cmd.py，**不放这里**：
    # 这块会持续长（套件间比较、按标签筛、矩阵跑），挤在 __main__ 里迟早变成第二个
    # web_server.py。接线只留三行。
    #
    # `--root` 缺省**当前目录**而不是 `~/.mat/workspace`：套件的首要消费者是
    # `mat run cases/x.suite.json`，它按 cwd 解析路径 ⇒ 这里跟它一致，避免
    # 「suite 改了一个、run 读的是另一个」。
    default_root = "."
    suite_p = sub.add_parser("suite", help="套件管理：列出 / 新建 / 加用例 / 移除 / 查看 / 校验")
    suite_sub = suite_p.add_subparsers(dest="suite_cmd", required=True)

    def _suite(name: str, help_: str, fn):
        p = suite_sub.add_parser(name, help=help_)
        p.add_argument("--root", default=".", help="工作区根目录（缺省当前目录）")
        p.set_defaults(func=fn, root=default_root)
        return p

    def _suite_guarded(fn):
        """把「文件找不到 / 套件名非法」翻译成一句能行动的话。

        不做这层的话，用户敲错一个 glob 就会看到一整屏 traceback——CLI 报错必须
        是人能照着改的（与 cmd_run 里 `except FileNotFoundError` 同一原则）。
        """
        def wrapper(args):
            try:
                return fn(args)
            except FileNotFoundError as exc:
                print(f"✗ {exc}")
                print("  提示：用例要放在工作区的 cases/ 下，或给 cases/x.json / glob；"
                      "先 `mat suite list` 看现有套件。")
                return 1
            except ValueError as exc:
                print(f"✗ {exc}")
                return 1
        return wrapper

    _suite("list", "列出工作区里的套件", _suite_guarded(suite_cmd.cmd_list))
    suite_new = _suite("new", "新建套件（--add 带上首批用例）", _suite_guarded(suite_cmd.cmd_new))
    suite_new.add_argument("name")
    suite_new.add_argument("--add", nargs="*", default=[], help="首批用例（可写 cases/x.json 或 glob）")
    suite_new.add_argument("--allow-empty", action="store_true", help="允许建空套件（默认拒绝，空套件跑不了）")
    suite_new.add_argument("--force", action="store_true", help="套件已存在时覆盖")
    suite_add = _suite("add", "往套件里加用例", _suite_guarded(suite_cmd.cmd_add))
    suite_add.add_argument("name")
    suite_add.add_argument("cases", nargs="+", help="用例文件或 glob")
    suite_rm = _suite("rm", "从套件里移除用例", _suite_guarded(suite_cmd.cmd_rm))
    suite_rm.add_argument("name")
    suite_rm.add_argument("cases", nargs="+", help="用例文件或 glob")
    _suite("show", "看套件详情（顺序即执行顺序）", _suite_guarded(suite_cmd.cmd_show)).add_argument("name")
    _suite("check", "校验套件（文件存在 / DSL 合法 / 包名一致）", _suite_guarded(suite_cmd.cmd_check)).add_argument("name")

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
