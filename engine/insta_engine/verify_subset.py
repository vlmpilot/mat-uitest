"""`mat verify --subset`：批量模式(协议 v4 §4.1)——子集选择 + 聚合 verdict。

## 为什么单独一层

单条 verify 与批量子集共享「执行」(run_verify),但批量多了三件自己的事:
**选择**(哪些用例该跑)、**聚合**(verdict 全序)、**预算**(总时长上限)。放
verify.py 会把它撑到 600 行;单独一层让选择算法可离线单测(不需要真机)。

## 聚合全序(先定 verdict 再推 exit,避免两个字段各算一套)

`error > fail > blocked > incomplete > pass`;**`selected == 0` ⇒ blocked**——
子集为空(smoke 无匹配 / last-failed 无失败)绝不能报 pass,那是最危险的假绿。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "mat.verify/1"

#: smoke 的优先级解析表(方案 §4.1 修订 2:**不是直接比较字符串**——
#: 导入链写的是 "P1"/"高" 这类字符串,直接 <= 1 会抛 TypeError)
_PRIORITY: dict[str, int] = {"P0": 0, "高": 0, "1": 0, "P1": 1, "中": 1}

#: 选择记录(artifacts/verify/_selection.json):按 (package, mode) 分键——
#: 否则上次 smoke 选出的少数几条会被下次 changed 复用(张冠李戴)
SELECTION_FILE = "_selection.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def case_priority(case: dict) -> int | None:
    """meta.priority → 0/1;无法解析的返回 None(不计入 smoke,不抛 TypeError)。"""
    raw = str((case.get("meta") or {}).get("priority") or "").strip()
    return _PRIORITY.get(raw)


def case_is_smoke(case: dict) -> bool:
    tags = (case.get("meta") or {}).get("tags") or []
    if any(str(t).strip().lower() == "smoke" for t in tags):
        return True
    prio = case_priority(case)
    return prio is not None and prio <= 1


# -- _history.json:每个 caseId 最近 3 条执行(增量,排除评测跑批) -----------------

HISTORY_FILE = "_history.json"
HISTORY_KEEP = 3


#: 评测跑批的目录前缀：`artifacts/eval/` 与**日期命名**的跑批目录
#: （`2026-10-25-d3-r1/` 这类，是我们自己跑评测的惯例命名）。
#: 2026-10-31 评审实测：2479 份 run.json 里只有 64 份在 `eval/`，日期命名的另有 798
#: 份 ⇒ 只挡 3%。约定：批量跑评测时把输出放进 `eval/` 或日期目录，日常用例不进这两类。
_BATCH_DIR_RE = r"^\d{4}-\d{2}-\d{2}"


def _is_batch_run(rel: str) -> bool:
    """`rel` 是相对 `artifacts/` 的路径；判断它是不是评测跑批（不该进日常索引）。"""
    if rel.startswith("eval/"):
        return True
    return bool(re.match(_BATCH_DIR_RE, rel.split("/", 1)[0]))


def build_history(root: Path, force: bool = False) -> dict:
    """扫描 artifacts/**/run.json,产出 {caseId: [最近 3 条]} 的持久索引。

    - **排除评测跑批**（2026-10-31 评审 P2-a）：评测与日常用例同 caseId,受控跑批的
      失败不该驱动日常 smoke/last-failed 的选择。判据见 `_is_batch_run`——只排
      `eval/` 不够（实测只挡 3%）;
    - 增量:按 run 目录 mtime 判断是否需要解析(现网 2479 份 run.json 全解析
      5.62s,不能每次付);索引文件记录已覆盖的 mtime 集合;
    - 每条只留 {status, finished_at, run_json}——last-failed 只需要这三个。
    """
    hist_path = Path(root) / "artifacts" / "verify" / HISTORY_FILE
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist = _load_json(hist_path) if not force else {}
    meta = hist.get("__meta__") or {}
    seen: dict[str, float] = {k: float(v) for k, v in (meta.get("mtimes") or {}).items()}
    out: dict[str, list[dict]] = hist.get("cases") or {}

    for run_json in sorted((Path(root) / "artifacts").rglob("run.json")):
        rel = run_json.relative_to(Path(root) / "artifacts").as_posix()
        if _is_batch_run(rel):
            continue  # 评测跑批不进日常索引(见上)
        mtime = run_json.stat().st_mtime
        if not force and seen.get(rel) == mtime:
            continue
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        case_id = str((data.get("case") or {}).get("caseId") or "")
        if not case_id:
            continue
        entry = {"status": (data.get("run") or {}).get("status") or "?",
                 "finished_at": data.get("finished_at") or "",
                 "run_json": str(run_json)}
        bucket = [e for e in out.get(case_id, []) if e["run_json"] != entry["run_json"]]
        bucket.append(entry)
        bucket.sort(key=lambda e: e["finished_at"], reverse=True)
        out[case_id] = bucket[:HISTORY_KEEP]
        seen[rel] = mtime

    result = {"__meta__": {"built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                           "mtimes": seen}, "cases": out}
    hist_path.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def last_failed(history: dict, case_ids: list[str]) -> list[str]:
    """每个 caseId 最近 3 次里出现过 failed/error ⇒ 进 last-failed。"""
    out = []
    for cid in case_ids:
        entries = (history.get(cid) or [])[:HISTORY_KEEP]
        if any(e.get("status") in ("failed", "error") for e in entries):
            out.append(cid)
    return out


# -- 子集选择 -----------------------------------------------------------------

def collect_cases(root: Path) -> list[Path]:
    """工作区 `cases/*.json`（排除 `.suite.json`）——CLI 与 MCP **共用**这份清单。

    为什么单独成函数（2026-10-31 终检接缝 #3）：MCP 要暴露 `--subset` 时，如果各自
    写一遍 glob 规则，两处迟早分叉（suite 文件要不要算、排序稳不稳定）。批量验证的
    「跑哪些文件」必须只有一个答案。
    """
    cases_dir = Path(root) / "cases"
    return [p for p in sorted(cases_dir.glob("*.json"))
            if not p.name.endswith(".suite.json")]


def graph_sha(root: Path, package: str) -> str:
    """目标包图谱的 sha256;没有图谱返回空串(changed 无记录 ⇒ 全量)。"""
    p = Path(root) / "artifacts" / "page_index" / f"{package}.json"
    if not p.exists():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def case_package(case: dict) -> str:
    """用例声明的目标包名(`target.package`),没有则空串。"""
    return str((case.get("target") or {}).get("package") or "").strip()


def select_subset(root: Path, cases: list[tuple[Path, dict]], mode: str,
                  package: str) -> tuple[list[tuple[Path, dict]], dict]:
    """从 (路径, 用例文档) 列表里选出子集;返回 (选中, selection 信息)。

    selection 落盘 `_selection.json`,按 (package, mode) 分键——changed 复用的
    就是这里记的 selected_ids。

    **先按包名过滤候选**（2026-10-31 评审 P0-1）：此前候选池是全部 `cases/*.json`,
    `--package` 只用于选择记录分键与图谱 sha ⇒ 要验 A 却跑 B 的用例（实测
    `--package <A 包>` 选出 2 条全属于 <B 包>）。配合 `--apk` 更糟：装 A 的包
    跑 B 的用例。现在只保留 `target.package` 完全相等的；没声明 target 的单独计数
    （无法确认属于该包，宁可跳过也不猜），跳过的数量写进 selection 让排查有据。
    """
    reasons: dict[str, list[str]] = {}
    selected: list[tuple[Path, dict]] = []
    sel_path = Path(root) / "artifacts" / "verify" / SELECTION_FILE
    selections = _load_json(sel_path)

    matched: list[tuple[Path, dict]] = []
    skipped_other = skipped_no_pkg = 0
    for item in cases:
        pkg = case_package(item[1])
        if pkg == package:
            matched.append(item)
        elif pkg:
            skipped_other += 1
        else:
            skipped_no_pkg += 1
    cases = matched
    considered = len(cases)

    if mode == "smoke":
        for path, case in cases:
            if case_is_smoke(case):
                selected.append((path, case))
                reasons.setdefault(str(case.get("caseId") or path.name),
                                   []).append("tag:smoke")
    elif mode == "last-failed":
        history = build_history(root)
        failed = set(last_failed(history, [str(c.get("caseId") or "") for _, c in cases]))
        for path, case in cases:
            cid = str(case.get("caseId") or "")
            if cid in failed:
                selected.append((path, case))
                reasons.setdefault(cid, []).append("last-failed")
    elif mode == "changed":
        sha = graph_sha(root, package)
        prev = (selections.get(package) or {}).get("changed") or {}
        if not sha or prev.get("graph_sha") != sha:
            selected = list(cases)
            mode_note = "graph-changed" if sha else "no-graph"
            for path, case in cases:
                reasons.setdefault(str(case.get("caseId") or path.name),
                                   []).append(mode_note)
        else:
            keep = set(prev.get("selected_ids") or [])
            for path, case in cases:
                cid = str(case.get("caseId") or "")
                if cid in keep:
                    selected.append((path, case))
                    reasons.setdefault(cid, []).append("reuse-selected")
    else:
        raise ValueError(f"未知子集模式: {mode}")

    # 记录本次选择(供 changed 复用与人工核对);changed 复用分支也刷新时间
    node = selections.setdefault(package, {})
    node[mode] = {"graph_sha": graph_sha(root, package),
                  "last_run_at": datetime.now(UTC).isoformat(timespec="seconds"),
                  "selected_ids": [str(c.get("caseId") or p.name) for p, c in selected]}
    sel_path.parent.mkdir(parents=True, exist_ok=True)
    sel_path.write_text(json.dumps(selections, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    info = {"mode": mode, "package": package, "considered": considered,
            "selected": len(selected), "reasons": reasons,
            # 被包名过滤挡掉的候选：agent/人排查「为什么只跑了 2 条」时先看这两个数
            "skipped_other_package": skipped_other,
            "skipped_no_package": skipped_no_pkg}
    return selected, info


# -- 聚合 ---------------------------------------------------------------------

_ORDER = ("error", "fail", "blocked", "incomplete", "pass")
_EXIT_BY_VERDICT = {"error": 1, "fail": 2, "blocked": 3, "incomplete": 4, "pass": 0}


def _aggregate_actions(results: list[dict]) -> list[dict]:
    """把逐条的失败分类汇总成**一条批量的下一步**（按出现次数降序）。

    2026-10-31 评审 P2-d：聚合只回 5 个键，`next_actions` 一条都没有——而批量恰恰是
    「改完代码跑一批」的主场景，等于把单条模式最有用的东西在批量里归零。每条动作带上
    `cases`，agent 才知道先翻哪几条的证据，不必自己再扫一遍 `cases[]`。
    """
    from .verify import NEXT_ACTIONS

    counts: dict[str, int] = {}
    where: dict[str, list[str]] = {}
    for r in results:
        cid = str(r.get("case_id") or "")
        for cat, n in (r.get("failed_categories") or {}).items():
            counts[cat] = counts.get(cat, 0) + max(int(n or 1), 1)
            where.setdefault(cat, []).append(cid)
        if r.get("verdict") == "blocked":
            # 拒跑不是「失败分类」，但同样需要下一步。注意两套命名的差别：
            # `blocked_reason` 是 device_busy（下划线，协议字段），而动作词表的键是
            # device-busy（连字符）——直接透传会落到 unknown 动作（实测踩过）。
            cat = str(r.get("blocked_reason") or "precondition").replace("_", "-")
            counts[cat] = counts.get(cat, 0) + 1
            where.setdefault(cat, []).append(cid)

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"category": cat, "count": n,
             "cases": sorted({c for c in where.get(cat, []) if c}),
             "action": NEXT_ACTIONS.get(cat, NEXT_ACTIONS["unknown"])}
            for cat, n in ordered]


def _empty_note(selection: dict) -> str:
    """空子集要说清**为什么空**——只说「不代表通过」，agent 还得自己猜是哪一步没命中。

    两种原因必须分开写（2026-10-31 复核）：**这个包没候选** vs **有候选但都不命中模式**。
    原实现只要有「被包名排除的其它 App 用例」就走「该包候选 0 条」那一支，而
    `skipped_other_package` 在主仓几乎恒 >0（28 条用例分属 6 个包）⇒ 实测
    `--subset smoke --package <A 包>`（该包 10 条候选、0 条标 smoke）
    被写成「该包候选 0 条……用 --package 选对包」——包名本来就是对的，
    这条把 agent 指向了错误的修法（去改 --package，而不是去补 smoke 标签）。
    """
    note = "子集为空,不代表通过——绝不能把「什么都没跑」当成「全过」"
    considered = int(selection.get("considered") or 0)
    skipped = int(selection.get("skipped_other_package") or 0)
    no_pkg = int(selection.get("skipped_no_package") or 0)
    excluded = [t for t in ((f"{skipped} 条属于其它 App" if skipped else ""),
                            (f"{no_pkg} 条未声明 target.package" if no_pkg else "")) if t]
    if considered:
        note += f";该包 {considered} 条候选都不满足该模式的命中条件"
        if excluded:
            note += f"(另有 {'、'.join(excluded)},已按包名排除)"
        if selection.get("mode") == "smoke":
            note += (";smoke 靠用例标签命中(meta.tags 含 smoke 或 priority<=1)——"
                     "给要验的关键流程打上标签,或改用 --subset last-failed")
        elif selection.get("mode") == "last-failed":
            note += (";last-failed 只选最近 3 次里出现过 failed/error 的用例——"
                     "这批都没失败过,说明不用重跑")
    else:
        note += ";该包候选 0 条"
        if excluded:
            note += (f"(另有 {'、'.join(excluded)},均已排除——用 --package 选对包,"
                     "或给用例补 target.package)")
    return note


def aggregate(mode: str, results: list[dict], selection: dict) -> dict:
    """按全序聚合子集结果(先定 verdict 再推 exit)。"""
    if not results:
        return {"schema": SCHEMA, "mode": "subset", "verdict": "blocked",
                "ok": False, "exit_code": 3,
                "summary": {"selected": 0, "passed": 0, "failed": 0, "blocked": 0,
                            "incomplete": 0},
                "selection": {**selection, "considered": selection.get("considered", 0)},
                "cases": [], "next_actions": [],
                "notes": [_empty_note(selection)]}
    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in _ORDER}
    verdict = next((v for v in _ORDER if counts[v] > 0), "pass")
    cases = [{k: r.get(k) for k in ("case_id", "verdict", "duration_ms",
                                    "failed_categories", "artifacts")}
             for r in results]
    out = {"schema": SCHEMA, "mode": "subset",
           "verdict": verdict, "ok": verdict == "pass",
           "exit_code": _EXIT_BY_VERDICT[verdict],
           "summary": {"selected": len(results), "passed": counts["pass"],
                       "failed": counts["fail"], "blocked": counts["blocked"],
                       "incomplete": counts["incomplete"]},
           "selection": selection, "cases": cases,
           "next_actions": _aggregate_actions(results),
           "queued": any(r.get("queued") for r in results),
           "wait_ms": max((r.get("wait_ms") or 0) for r in results),
           "verified_at": datetime.now(UTC).isoformat(timespec="seconds")}
    return out


def _install_failure(mode: str, selection: dict, install_info: dict) -> dict:
    """装包失败的统一出口：`error/1` + install 明细 + **install 分类的下一步**。

    与单条模式（`verify._install_error_verdict`）给同一个结论形状——否则 agent 得按
    「单条还是批量」分两套读法。`summary.selected` 记 0：一条都没跑，不能让它看起来
    像「跑了 N 条都出错」。
    """
    from .verify import NEXT_ACTIONS

    return {"schema": SCHEMA, "mode": "subset", "verdict": "error", "ok": False,
            "exit_code": 1,
            "summary": {"selected": 0, "passed": 0, "failed": 0, "blocked": 0,
                        "incomplete": 0},
            "selection": {**selection, "install_error": install_info.get("error")},
            "cases": [], "install": install_info,
            "next_actions": [{"category": "install",
                              "action": NEXT_ACTIONS["install"]}],
            "notes": ["装包失败——整体 error，一条都没跑；不要用这个结果判断用例质量"]}


def _persist_subset(root: Path, agg: dict) -> None:
    """批量聚合落盘（`artifacts/verify/_last_subset.json`）。

    逐条的判定已由 `run_verify` 写进各自产物目录；这份是**整批的结论**（聚合 verdict +
    summary + next_actions），控制台与人都靠它回答「这批到底过没过、下一步先修哪个」。
    写盘失败静默——和单条一样，落盘是附加价值，不能影响判定。
    """
    try:
        directory = Path(root) / "artifacts" / "verify"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "_last_subset.json").write_text(
            json.dumps(agg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass


# -- 批量执行(逐条 run_verify + 预算)-------------------------------------------

def run_subset(root: Path, cases: list[Path], *, mode: str, package: str,
               serial: str | None = None, platform: str = "android",
               allow_unmet: bool = False, quality_gate: bool = True,
               timeout_s: float | None = 180, apk: Path | None = None,
               reinstall: bool = False, max_duration: float | None = 300,
               artifacts: Path | None = None, on_progress=None) -> dict:
    """批量验证:选择 → (装包一次) → 逐条 → 聚合。

    - 装包在**首条之前装一次**(幂等 -r;装包失败 ⇒ 整体 error,评审 §4.1);
    - 总时长预算 `max_duration`(默认 300s):超出 ⇒ 剩余任务记 incomplete,
      聚合按全序出 verdict——**超时不报 pass**,agent 知道还有没跑的;
    - 逐条复用 run_verify(timeout_s/守卫/装包语义同单条);
    - `artifacts` 是**根目录**:每条用例落 `<artifacts>/<用例文件名>/`,避免互相覆盖。
      此前 CLI 传了 `artifacts` 而本函数不收 ⇒ `TypeError`(批量入口整体不可用)。
    """
    from . import verify as verify_mod

    all_cases = []
    for p in cases:
        try:
            all_cases.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    selected, selection = select_subset(root, all_cases, mode, package)

    install_info = None
    if apk is not None and selected:
        from .adb import adb as _adb
        # 装包能力与单条验证同源（verify.install_apk）：两条路径各写一套迟早会分叉
        try:
            install_info = verify_mod.install_apk(root, _adb, serial, apk, package,
                                                  reinstall=reinstall)
        except ValueError as exc:
            # `install_apk` 对「工作区外路径 / 非 .apk」抛 ValueError（可行动的报错）。
            # 单条模式一直捕获，批量模式此前漏了 ⇒ 用户看到的是栈回溯、没有 JSON
            # （2026-10-31 评审 P0-2）。协议要求所有失败都给结论，所以在这里收口。
            install_info = {"ok": False, "pkg": package, "error": str(exc)}
        if not install_info["ok"]:
            agg = _install_failure(mode, selection, install_info)
            _persist_subset(root, agg)
            return agg

    results: list[dict] = []
    budget_left = max_duration
    for path, _case in selected:
        if budget_left is not None and budget_left <= 0:
            results.append({"case_id": str(_case.get("caseId") or path.stem),
                            "verdict": "incomplete", "duration_ms": 0,
                            "failed_categories": {}, "artifacts": {},
                            "note": "预算用尽未执行"})
            continue
        t0 = time.perf_counter()
        case_id = str(_case.get("caseId") or path.stem)
        code, verdict = verify_mod.run_verify(
            root, path, serial=serial, platform=platform,
            allow_unmet=allow_unmet, quality_gate=quality_gate,
            timeout_s=timeout_s,
            artifacts=(Path(artifacts) / path.stem) if artifacts else None)
        spent = time.perf_counter() - t0
        if budget_left is not None:
            budget_left -= spent
        results.append({"case_id": verdict.get("case_id") or case_id,
                        "verdict": verdict["verdict"], "duration_ms":
                        verdict.get("duration_ms") or round(spent * 1000),
                        "failed_categories": verdict.get("failed_categories") or {},
                        "artifacts": verdict.get("artifacts") or {},
                        # 拒跑原因要带上来：聚合的 next_actions 靠它区分
                        # precondition / device-busy，否则只知道「有 blocked」却不知道修什么
                        "blocked_reason": verdict.get("blocked_reason"),
                        "queued": verdict.get("queued"),
                        "wait_ms": verdict.get("wait_ms")})
        if on_progress:
            on_progress(path, verdict)
    agg = aggregate(mode, results, selection)
    if install_info is not None:
        agg["install"] = install_info
    _persist_subset(root, agg)
    return agg
