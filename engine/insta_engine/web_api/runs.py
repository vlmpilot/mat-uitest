"""执行域长任务：/api/run、/api/explore、/api/gen（经 start_job 后台执行）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..dsl import TestCase, dump_dsl, unique_case_id
from ..recording import make_recorder
from ..report import render_html
from ..runner import Runner, RunResult
from .runtime import _require_fresh_code, device_lock, get_driver, operator_of, start_job

#: 拆分生成单次落盘条数上限（prompt 约束 ≤5 条，代码再兜一层失控输出；2026-10-13）
MAX_SPLIT = 8


def api_run(root: Path, body: dict) -> tuple[int, dict]:
    app_filter = str(body.get("app", "")).strip()
    case_name = str(body.get("case", "")).strip()
    cases_param = body.get("cases")
    raw: dict | None = None
    if (isinstance(cases_param, list) and cases_param
            and not app_filter and not case_name):
        # 选中多条用例批量执行（发起人 2026-10-12 用例列表多选）：内存构建临时套件
        # 走与套件完全相同的执行/聚合报告链路——**不落盘**，避免每次勾选运行都往
        # cases/ 丢临时文件污染用例列表。
        names: list[str] = []
        for item in cases_param:
            n = str(item).strip()
            if not n or ".." in n or n.startswith("/"):
                return 400, {"error": f"非法用例名: {n}"}
            names.append(n)
        missing = [n for n in names if not (root / "cases" / n).exists()]
        if missing:
            return 400, {"error": f"用例不存在: {', '.join(missing)}"}
        raw = {"name": f"选中 {len(names)} 条用例", "cases": names}
        case_name = "_batch_selected.json"
        case_path = root / "cases" / case_name
    elif app_filter and not case_name:
        # 批量执行：按 target.package 过滤 cases/ 全部用例（与 CLI --app 同语义）
        matched = [f for f in sorted((root / "cases").glob("*.json"))
                   if json.loads(f.read_text(encoding="utf-8")).get("target", {}).get("package") == app_filter]
        if not matched:
            return 400, {"error": f"没有 target.package={app_filter} 的用例"}
        suite = {"name": f"app:{app_filter}", "cases": [f.name for f in matched]}
        case_name = f"_batch_{app_filter.replace('.', '_')}.json"
        case_path = root / "cases" / case_name
        case_path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    else:
        case_path = root / "cases" / case_name
    if raw is None:
        if ".." in case_name or case_name.startswith("/"):
            return 400, {"error": f"非法用例名: {case_name}"}
        if not case_path.exists():
            # 兼容 "cases/x.json" 前缀形态（套件条目/直接路径；".."/绝对路径已被上方拒绝）
            alt = root / case_name
            if alt.exists():
                case_path = alt
        if not case_path.exists():
            return 400, {"error": f"用例不存在: {case_name}"}
        try:
            raw = json.loads(case_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return 400, {"error": f"用例格式错误: {exc}"}
    # 套件判定：文件含 cases 数组 ⇒ 套件模式（与 CLI mat run 套件模式同构）
    suite_items = raw.get("cases") if isinstance(raw.get("cases"), list) else None
    is_suite = suite_items is not None
    case = None
    if not is_suite:
        try:
            case = TestCase.model_validate(raw)
        except ValueError as exc:
            return 400, {"error": f"用例格式错误: {exc}"}
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    record = bool(body.get("record")) and not (case and case.datasets)  # 数据驱动暂不支持录屏（诚实降级）
    # 套件用例间重置（发起人 2026-10-12：16 条导入用例顺序执行零重置，08 失败后
    # 09-16 的前置页面全部不可达、雪崩连锁失败）。cold = 每条开始前 force-stop
    # 目标 App，冷启动交给用例自身的 launch 前置（导入转换产物已自动注入）。
    reset_mode = str(body.get("reset") or "cold")
    artifacts = root / "artifacts" / case_path.stem

    def job(emit, cancel):
        _require_fresh_code()
        # **驱动/Runner 的创建必须在 job 内**：无设备时 `get_driver` 抛 `AdbError`，
        # 放在 job 外会让端点直接 **500**，而契约是"优雅落 error + 事件流可查"。
        # ⚠️ 这个洞**一直被掩盖**：跑测试时设备总是插着，走不到这个分支。
        # 2026-10-06 设备掉线后 `test_run_job_fails_gracefully_without_device` 才红。
        recorder = None
        if record:
            recorder, rec_reason = make_recorder(platform, serial, artifacts)
            if recorder is None:
                # 录屏不可用必须**可见**（事件流+报告），而不是静默按无录屏跑——
                # 用户勾了录屏却看不到视频 = 上一条抱怨的根源。
                emit("record", "disabled", f"录屏不可用：{rec_reason}")
        try:
            runner = Runner(get_driver(serial, platform), artifacts, recorder=recorder)
            runner.operator = operator_of(body)  # 执行人落 run.json/报告（发起人 2026-10-12）
        except Exception as exc:
            # 先 emit 再抛：worker 会把 job 置 error，但**事件流要非空**才可查
            emit("run", "error", f"设备不可用：{type(exc).__name__}: {exc}")
            raise
        lock = device_lock(serial)
        if not lock.acquire(blocking=False):
            default_dev = serial or "默认设备"
            emit("run", "queued", f"设备忙（{default_dev}），排队等待前一任务完成")
            lock.acquire()  # 串行等待（评审 P1-4：同设备并发=互抢点击→不可信报告）
        try:
            emit("run", "running", case_path.name)

            # 日志增强（发起人需求：日志要能看到「点击了什么元素、到达了什么页面」）：
            # 元素来自用例步骤的 target；到达页取 foreground_app（每步一次轻量调用，
            # 拿不到就略过——绝不能因为日志增强影响执行）。工厂化：套件模式逐条
            # 前缀「<序号>_<用例>:」区分同名步骤。
            def _make_on_step(step_by_id, drv, prefix=""):
                def _on_step(result):
                    status = result.status
                    if status == "passed" and (result.retried_on_failure or result.heal):
                        status = "healed" # ：自愈/重试后通过单独标记
                    bits: list[str] = []
                    st = (step_by_id or {}).get(result.step_id)
                    if st is not None and st.target is not None:
                        t = st.target
                        if st.action.value == "input":
                            verb = "输入到"
                        elif st.action.value in ("tap", "long-press"):
                            verb = "点击"
                        else:
                            verb = "操作"
                        bits.append(f"{verb} [{t.strategy.value}={t.value}]")
                    try:
                        fa = drv.foreground_app() or {}
                        page = fa.get("activity") or fa.get("package") or ""
                        if page:
                            bits.append(f"到达 {page.split('.')[-1]}")
                    except Exception:
                        pass
                    err = result.error or result.warning or ""
                    if err:
                        bits.append(f"⚠ {err[:160]}")
                    emit(f"{prefix}{result.step_id}", status, " · ".join(bits) or "—",
                         result.duration_ms, result.screenshot)
                return _on_step

            if is_suite:
                # 套件模式：逐条执行 + 聚合报告（与 CLI 套件模式同构；设备锁整段持有）
                from ..report import render_index_html
                entries: list[tuple[str, str, RunResult]] = []
                total = len(suite_items)
                # 批次标识：报告页把同批条目归组折叠
                batch_id = f"{case_path.stem}#{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                for index, item in enumerate(suite_items, 1):
                    if cancel.is_set():
                        break
                    item_path = root / "cases" / str(item)
                    if not item_path.exists():
                        item_path = root / str(item)  # 兼容带目录前缀的条目
                    case_i = TestCase.model_validate_json(item_path.read_text(encoding="utf-8"))
                    stem = Path(str(item)).stem
                    art_i = root / "artifacts" / f"{index:02d}_{stem}"
                    rec_i = make_recorder(platform, serial, art_i)[0] \
                        if record and not case_i.datasets else None
                    runner_i = Runner(get_driver(serial, platform), art_i,
                                      recorder=rec_i)
                    runner_i.operator = operator_of(body)  # 执行人随每条 run.json 落盘
                    # 批次归组：写进每条 run.json 的 run.batch（报告页聚合展示）
                    runner_i.batch = {"id": batch_id, "suite": case_path.stem,
                                      "index": index, "total": total}
                    if reset_mode == "cold":
                        # 用例间重置（cold）：force-stop 后冷启动由用例自身的
                        # launch(cold) 前置完成；无 launch 前置的老用例首步会显式
                        # 失败（诚实暴露缺前置），而非静默依赖上一条遗留状态。
                        pkg_i = (case_i.target or {}).get("package") or ""
                        if pkg_i:
                            try:
                                runner_i.drv.app_stop(pkg_i)
                                emit(stem, "running", f"已重置: force-stop {pkg_i}")
                            except Exception as exc:  # 重置失败不阻断（如 iOS 未安装/未运行）
                                emit(stem, "running", f"重置跳过: {type(exc).__name__}")
                    emit(stem, "running", f"（{index}/{total}）{case_i.name or case_i.case_id}")
                    run_i = runner_i.run(
                        case_i,
                        on_step=_make_on_step({st.id: st for st in case_i.steps},
                                              runner_i.drv, prefix=f"{index:02d}_{stem}:"),
                        cancel_event=cancel)
                    render_html(run_i, art_i)
                    # rel 相对**聚合页所在目录**（artifacts/<套件名>/）——条目产物刻意写在
                    # artifacts/ 顶层（list_runs 一层 glob 才扫得到，批次归组靠 run.batch），
                    # 二者是兄弟目录 ⇒ 链接必须带 ../ 前缀，否则聚合页点「报告」404
                    # （2026-10-13 实测：_batch_selected 聚合页链接解析成批内子路径）
                    entries.append((case_i.name or case_i.case_id,
                                    f"../{art_i.name}/report.html", run_i))
                    emit(stem, run_i.status, f"（{index}/{total}）完成")
                render_index_html(case_path.stem, entries, artifacts)
                passed_n = sum(1 for _, _, r in entries if r.status == "passed")
                return (f"套件 {passed_n}/{len(entries)} 通过"), \
                    f"/artifacts/{case_path.stem}/index.html"

            step_by_id = {st.id: st for st in case.steps}
            _on_step = _make_on_step(step_by_id, runner.drv)

            if case.datasets:  # 数据驱动用例与 CLI 行为一致（双入口同构）
                from ..report import render_index_html
                ds_root = root / "artifacts" / f"{case_path.stem}-ds"
                entries = []
                for name, run, ds_artifacts in runner.run_datasets(
                        case, ds_root, on_step=_on_step, cancel_event=cancel):
                    render_html(run, ds_artifacts)
                    entries.append((name, f"{ds_artifacts.name}/report.html", run))
                render_index_html(case.name or case.case_id, entries, ds_root)
                return (f"{len(entries)} 组 · 通过 "
                        f"{sum(1 for _, _, r in entries if r.status == 'passed')}"), \
                    f"/artifacts/{case_path.stem}-ds/index.html"
            run = runner.run(case, on_step=_on_step, cancel_event=cancel)
            render_html(run, artifacts)
            if run.status == "cancelled":
                return f"已中止（完成 {len(run.steps)}/{len(case.steps)} 步）", \
                    f"/artifacts/{artifacts.name}/report.html"
            return f"{run.status}（步骤 {run.step_success_rate:.0%}）", f"/artifacts/{artifacts.name}/report.html"
        finally:
            lock.release()

    return 200, {"job_id": start_job("run", job, meta={"serial": serial, "platform": platform,
                                                       "operator": operator_of(body)})}


def api_explore(root: Path, body: dict) -> tuple[int, dict]:
    package = str(body.get("package", "")).strip()
    minutes = min(max(float(body.get("minutes", 2)), 0.5), 10)
    steps = min(max(int(body.get("steps", 30)), 5), 120)
    max_depth = min(max(int(body.get("max_depth", 3)), 1), 6)
    if not package:
        return 400, {"error": "需要 package"}
    # A3（2026-10-12 评审）：包名会拼进 artifacts/索引路径——形状非法（../、绝对
    # 路径、盘符）在边界拒绝，不给越界读写原语。
    from ..pkgname import safe_package_name

    try:
        safe_package_name(package)
    except ValueError as exc:
        return 400, {"error": f"包名不合法: {exc}"}
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    if platform == "ios":
        # 与 CLI 同口径的**诚实拦截**：explore 依赖页面图谱，其 WDA 适配在 P3。
        # 不拦会在未验证路径上产出"看着正常、实际无效"的图谱（评审 I4 /）。
        return 400, {"error": "iOS 平台暂不支持 explore（页面图谱的 WDA 适配在 P3）"
                              "——当前 iOS 可用：mat run --platform ios"}

    def job(emit, cancel):
        import uuid as _uuid

        from ..explore import explore as _explore
        from ..report import render_explore_html
        _require_fresh_code()
        lock = device_lock(serial)
        if not lock.acquire(blocking=False):
            emit("explore", "queued", f"设备忙（{serial or '默认设备'}），排队等待")
            lock.acquire()
        try:
            explore_id = datetime.now(UTC).strftime("%H%M%S") + "-" + _uuid.uuid4().hex[:4]
            artifacts = root / "artifacts" / "explore" / explore_id
            index_path = root / "artifacts" / "page_index" / f"{package}.json"
            report = _explore(get_driver(serial, platform), package, minutes=minutes,
                              max_steps=steps, artifacts_dir=artifacts, max_depth=max_depth,
                              index_path=index_path,
                              # 探索进度进事件流（发起人 2026-10-12：探索任务日志区一直
                              # 显示"等待事件…"——job 包装此前没把 emit 接进 explore 的
                              # on_event 回调，任务全程 0 事件）
                              on_event=lambda done, total: emit(
                                  "explore", "running", f"探索进度 {done}/{total} 步"))
            render_explore_html(report, artifacts)
            return (f"步数 {report['steps']} · 覆盖页面 {report['page_count']} · "
                    f"发现异常 {len(report['findings'])} 个"), \
                f"/artifacts/explore/{explore_id}/report.html"
        finally:
            lock.release()

    return 200, {"job_id": start_job("explore", job, meta={"serial": serial, "platform": platform,
                                                           "operator": operator_of(body)})}


def api_gen(root: Path, body: dict) -> tuple[int, dict]:
    from ..ai_client import GenClient
    from ..page_index import controls_context_for, graph_context_for

    nl = str(body.get("nl", "")).strip()
    package = str(body.get("package", "")).strip()
    if not nl or not package:
        return 400, {"error": "需要 nl 与 package"}
    base_dir = root / "artifacts" / "page_index"
    app_info: dict = {"package": package}
    controls = controls_context_for(package, base_dir)
    if controls:
        app_info["controls"] = controls
    graph = graph_context_for(package, base_dir)
    if graph:
        app_info["graph"] = graph
    cases_dir = root / "cases"

    def job(emit, cancel):
        _require_fresh_code()
        from ..quota import UsageStore

        usage = UsageStore()
        usage.ensure_capacity()  # 预检（T53b：模型失败不扣额度；web 端此前漏配额，一并补上）
        emit("gen", "running", "AI 生成中（同页多个入口将拆分为多个用例）")
        cases, warns = GenClient.from_env().generate_cases(nl, app_info)
        cases_dir.mkdir(parents=True, exist_ok=True)  # 循环外建一次（严格环境 EEXIST 教训）
        stamp = datetime.now().strftime("%H%M%S")
        truncated = max(0, len(cases) - MAX_SPLIT)
        outs: list[str] = []
        total_steps = 0
        for i, case in enumerate(cases[:MAX_SPLIT], 1):
            case.case_id = unique_case_id(case.case_id, cases_dir)
            case.meta["split_batch"] = stamp  # 同一次生成的批次溯源（同前缀文件名可追）
            for gate_issue in case.meta.get("quality_issues") or []:
                warns.append(f"{case.name or case.case_id}: {gate_issue}")
            path = cases_dir / f"gen-{stamp}-{i}.json"
            path.write_text(dump_dsl(case), encoding="utf-8")
            outs.append(path.name)
            total_steps += len(case.steps)
        for w in warns:  # 快速查看入口：质量问题逐条进事件流，不用翻 JSON meta
            emit("quality", "warning", w)
        snapshot = usage.consume()  # 成功落盘后才核销（1 次/任务，不随拆分条数增长）
        tail = f"；⚠️ {len(warns)} 条质量问题（详情见下方日志）" if warns else ""
        if truncated:
            tail += f"；超出上限已截断 {truncated} 条"
        # 文件名后追加 #展示编号+用例标题（发起人 2026-10-13）：文件名随机难对上号，
        # 编号/标题与用例列表同源（case_meta 惰性分配，同锁同注册表，幂等无副作用）
        from .workspace import case_meta

        meta = case_meta(root)
        def _desc(name: str) -> str:
            m = meta.get(name) or {}
            title = str(m.get("title") or "")
            return f"{name}(#{m.get('no')} {title})" if m.get("no") and title else name
        # 标准版(gen_limit=null)不限额——完成提示不再带「本月生成用量」（与入口提示同规则）
        usage_bit = ("，本月生成用量 "
                     f"{snapshot['gen_count']}/{snapshot['gen_limit']}"
                     ) if snapshot["gen_limit"] is not None else ""
        return (f"已生成 {len(outs)} 个用例（共 {total_steps} 步{usage_bit}）→ "
                f"cases/{'、'.join(_desc(o) for o in outs)}{tail}", None)

    return 200, {"job_id": start_job("gen", job, meta={"platform": "android",
                                                       "operator": operator_of(body)}), "outs": []}
