"""执行域长任务：/api/run、/api/explore、/api/gen（经 start_job 后台执行）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .. import promptstore
from ..dsl import TestCase, dump_dsl, unique_case_id
from ..preconditions import refused_result, unprovisionable
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
    rerun_dir = str(body.get("rerun_dir", "")).strip()
    if rerun_dir:
        # 单条回放（2026-10-25）：用**报告产物自带的完整用例数据**重跑——
        # 不依赖 cases/ 源文件（改名/删除/同名 caseId 多文件都不影响）；
        # 平台由调用方透传（报告页从 run.json 顶层 platform 带出）。
        if ".." in rerun_dir or rerun_dir.startswith("/"):
            return 400, {"error": f"非法 rerun_dir: {rerun_dir}"}
        run_json = root / "artifacts" / rerun_dir / "run.json"
        if not run_json.exists():
            return 404, {"error": f"找不到执行产物: artifacts/{rerun_dir}/run.json"}
        try:
            raw = json.loads(run_json.read_text(encoding="utf-8")).get("case")
        except (OSError, ValueError) as exc:
            return 400, {"error": f"读取执行产物失败: {exc}"}
        if not isinstance(raw, dict):
            return 400, {"error": "执行产物里没有用例数据（旧产物或探索任务）"}
        case_name = f"_rerun_{Path(rerun_dir).name}.json"
        case_path = root / "cases" / case_name
    elif (isinstance(cases_param, list) and cases_param
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
        # 批量执行：按 target.package 过滤 cases/ 全部用例（与 CLI --app 同语义）。
        # 内存构建临时套件、**不落盘**（2026-10-24 修，评审附带项：对齐多选分支与
        # CLI 10-23 的改法）——此前每次批量都往 cases/ 写 `_batch_<pkg>.json`，
        # 污染用例列表（state 列表只滤 .suite.json）、坏了还会让列表接口 500。
        matched = [f for f in sorted((root / "cases").glob("*.json"))
                   if json.loads(f.read_text(encoding="utf-8")).get("target", {}).get("package") == app_filter]
        if not matched:
            return 400, {"error": f"没有 target.package={app_filter} 的用例"}
        raw = {"name": f"app:{app_filter}", "cases": [f.name for f in matched]}
        case_name = f"_batch_{app_filter.replace('.', '_')}.json"
        case_path = root / "cases" / case_name
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
    # 分阶段套件（2026-10-21）：控制台的套件执行循环还是"平铺 cases"同构实现，
    # 不认 stages ⇒ 如实拒绝并指路，而不是报"用例格式错误"让人以为文件坏了。
    if (isinstance(raw, dict) and isinstance(raw.get("stages"), list)
            and "steps" not in raw):
        return 400, {"error": "分阶段套件（stages）暂只支持命令行 `mat run`；控制台请用平铺 cases 套件"}
    case = None
    if not is_suite:
        try:
            case = TestCase.model_validate(raw)
        except ValueError as exc:
            return 400, {"error": f"用例格式错误: {exc}"}
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    # 录屏（P1-5，2026-10-24 起数据驱动同样支持——逐组挂 recorder）：
    # 此前数据驱动被硬性禁用且**无声**（record 被置 False 后连 disabled 提示都
    # 不触发），用户勾了录屏跑数据组 = 无视频无提示。
    record = bool(body.get("record"))
    # 前置拒跑的放行开关（P1-7，与 CLI --allow-unmet-preconditions 同语义）：
    # 用户先手工把设备置成该状态后再勾选放行，而不是让闸门变成永远打不开的门。
    allow_unmet = bool(body.get("allowUnmetPreconditions"))
    # 套件用例间重置（发起人 2026-10-12：16 条导入用例顺序执行零重置，08 失败后
    # 09-16 的前置页面全部不可达、雪崩连锁失败）。cold = 每条开始前 force-stop
    # 目标 App，冷启动交给用例自身的 launch 前置（导入转换产物已自动注入）。
    reset_mode = str(body.get("reset") or "cold")
    artifacts = root / "artifacts" / case_path.stem

    def job(emit, cancel):
        _require_fresh_code()
        # 前置拒跑（P1-7，2026-10-24）：与 CLI `_blocked_preconditions` 同口径——
        # 带 MAT 无法置备前置（否定式权限态）的用例**不跑**。跑下去只会产出一条
        # 假的定位失败报告，用户按报告排查、方向从源头就错。产物与 CLI 同源
        # （preconditions.refused_result）：skipped 条目 + 最小报告，不占设备锁。
        if case is not None and not allow_unmet:
            blocked = unprovisionable(list(case.preconditions or []))
            if blocked:
                emit("run", "refused", f"前置条件无法置备：{'、'.join(blocked)}（已拒跑，详见报告）")
                refused_result(case, blocked, artifacts)
                return ("已拒跑：前置条件 MAT 无法置备（详见报告与处置建议）"), \
                    f"/artifacts/{artifacts.name}/report.html"
        # **驱动/Runner 的创建必须在 job 内**：无设备时 `get_driver` 抛 `AdbError`，
        # 放在 job 外会让端点直接 **500**，而契约是"优雅落 error + 事件流可查"。
        # ⚠️ 这个洞**一直被掩盖**：跑测试时设备总是插着，走不到这个分支。
        # 2026-10-06 设备掉线后 `test_run_job_fails_gracefully_without_device` 才红。
        # ⚠️ 套件模式**不在 job 开头建初始 Runner/录屏器**（2026-10-24，P1-7）：
        # 套件循环逐条自建 `runner_i`/`rec_i`，初始那份是死工作，而且它取设备
        # 会挡在逐条前置拒跑**之前**——拒跑本不需要设备。
        runner = None
        recorder = None
        if not is_suite:
            if record and not (case and case.datasets):
                recorder, rec_reason = make_recorder(platform, serial, artifacts)
                if recorder is None:
                    # 录屏不可用必须**可见**（事件流+报告），而不是静默按无录屏跑——
                    # 用户勾了录屏却看不到视频 = 上一条抱怨的根源。
                    emit("record", "disabled", f"录屏不可用：{rec_reason}")
            try:
                runner = Runner(get_driver(serial, platform), artifacts, recorder=recorder,
                                quality_gate=True,
                                workspace_root=root)  # 图谱/校准规则按控制台当前工作区读取（P1-3）
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
                    # 前置拒跑（P1-7）：套件逐条与 CLI `_refused_entry` 同口径——
                    # refused 条目 emit skipped 事件、聚合页按普通条目可见（不占设备时间）。
                    if not allow_unmet:
                        blocked_i = unprovisionable(list(case_i.preconditions or []))
                        if blocked_i:
                            emit(stem, "skipped",
                                 f"（{index}/{total}）拒跑：前置条件无法置备（{'、'.join(blocked_i)}）")
                            run_i = refused_result(case_i, blocked_i, art_i)
                            entries.append((case_i.name or case_i.case_id,
                                            f"../{art_i.name}/report.html", run_i))
                            continue
                    rec_i = make_recorder(platform, serial, art_i)[0] \
                        if record and not case_i.datasets else None
                    runner_i = Runner(get_driver(serial, platform), art_i,
                                      quality_gate=True,
                                      recorder=rec_i, workspace_root=root)
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

                def _rec_factory(art_dir, _platform=platform, _serial=serial):
                    # P1-5：数据驱动逐组挂录屏（此前无声禁用）；不可用时逐组 emit 可见
                    rec, reason = make_recorder(_platform, _serial, art_dir)
                    if rec is None and reason:
                        emit("record", "disabled", f"录屏不可用：{reason}")
                    return rec

                for name, run, ds_artifacts in runner.run_datasets(
                        case, ds_root, on_step=_on_step, cancel_event=cancel,
                        recorder_factory=_rec_factory if record else None):
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

    job_id = start_job("run", job, meta={"serial": serial, "platform": platform,
                                         "operator": operator_of(body)})
    # live：实时视图页（人看的旁路：投屏+事件流）。agent/前端可据此直接给用户
    # 一个可点链接；同源场景（控制台/agent 面板）相对路径即可。
    return 200, {"job_id": job_id, "live": f"/live/{job_id}"}


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

    job_id = start_job("explore", job, meta={"serial": serial, "platform": platform,
                                             "operator": operator_of(body)})
    return 200, {"job_id": job_id, "live": f"/live/{job_id}"}


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
    # 负责人对该应用的补充说明（app_notes/<包名>.md，黑/白名单等）——随 {app_info} 进 prompt
    from ..appnotes import for_prompt as _app_notes_for_prompt

    user_notes = _app_notes_for_prompt(root, package)
    if user_notes:
        app_info["user_notes"] = user_notes
    cases_dir = root / "cases"

    def job(emit, cancel):
        _require_fresh_code()
        from ..quota import UsageStore

        usage = UsageStore()
        emit("gen", "running", "AI 生成中（同页多个入口将拆分为多个用例）")
        cases, warns = GenClient.from_env().generate_cases(
            nl, app_info, index_dir=root / "artifacts" / "page_index")
        cases_dir.mkdir(parents=True, exist_ok=True)  # 循环外建一次（严格环境 EEXIST 教训）
        stamp = datetime.now().strftime("%H%M%S")
        truncated = max(0, len(cases) - MAX_SPLIT)
        outs: list[str] = []
        total_steps = 0
        # 文件名直接用用例标题（2026-10-25 用户诉求：不用再改一次名）；批内/磁盘撞名
        # 加 -2/-3（同标题多次生成是合法输入）。批次溯源在 meta.split_batch，不靠文件名。
        from ..manual_import import slugify_filename

        used_stems: set[str] = set()
        for i, case in enumerate(cases[:MAX_SPLIT], 1):
            case.case_id = unique_case_id(case.case_id, cases_dir)
            case.meta["split_batch"] = stamp  # 同一次生成的批次溯源
            for gate_issue in case.meta.get("quality_issues") or []:
                warns.append(f"{case.name or case.case_id}: {gate_issue}")
            stem = slugify_filename(case.name, fallback=f"gen-{stamp}-{i}")
            path = cases_dir / f"{stem}.json"
            n = 2
            while path.exists() or path.stem in used_stems:
                path = cases_dir / f"{stem}-{n}.json"
                n += 1
            used_stems.add(path.stem)
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
        # 官方代理才报「本月生成用量」；自带 Key 的用量不占平台（token 用户自付）
        if promptstore.is_server():
            usage_bit = ("，本月生成用量 "
                         f"{snapshot['gen_count']}/{snapshot['gen_limit']}"
                         ) if snapshot["gen_limit"] is not None else ""
        else:
            usage_bit = "，自带 Key 不占平台额度"
        return (f"已生成 {len(outs)} 个用例（共 {total_steps} 步{usage_bit}）→ "
                f"cases/{'、'.join(_desc(o) for o in outs)}{tail}", None)

    return 200, {"job_id": start_job("gen", job, meta={"platform": "android",
                                                       "operator": operator_of(body)}), "outs": []}
