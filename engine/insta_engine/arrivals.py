"""arrival 试跑验证：图谱记下的到达路径，试跑 N 轮才可信。

盲区：explore 记下 depth/parent_key 就"当数了"，从没验证过这条
路径**是否稳定可复现**——一条偶发路径会被当成可靠复现路径写进报告。

做法：对图谱里每条父链（L0 → … → 目标页），冷启动后**按 parent_action 文案逐跳重放**，
每跳核对落点 page_key 是否与图谱一致；连跑 trials 轮，通过 ≥ trials-1 轮 → `stable`，
否则 `unstable`（字段 rounds/passed/success_rate/status/reason）。

成本是真机时间（无 AI 成本）；点击预算 `click_budget` 兜底，防长链 × 多轮把设备绑死。
与 explore 的关系：explore 负责"发现路径"，本模块负责"证明路径"——互不调用，
只通过图谱文件（artifacts/page_index/<pkg>.json）交接。
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from .page_index import load_index, page_key, save_index
from .ui_tree import parse_nodes

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

_MAX_PATH_LEN = 6  # 超过 6 跳的链路重放性价比太低（每跳都是真机时间 + 失败放大）


def _chain_of(target: dict, pages_by_key: dict[str, dict]) -> list[dict] | None:
    """目标页的父链 → 重放序列 [{expected_key, action}]（L0 不含，目标页含）。

    链上有缺环（parent 页不在图谱 / 中途缺 parent_action）→ None（无法重放）。
    """
    chain: list[dict] = []
    seen: set[str] = set()
    cur: dict | None = target
    while cur is not None and cur.get("parent_key"):
        if cur["page_key"] in seen:
            return None  # 环（理论不该出现，防御）
        seen.add(cur["page_key"])
        action = cur.get("parent_action")
        if not action:
            return None
        chain.append({"expected_key": cur["page_key"], "action": action})
        cur = pages_by_key.get(cur["parent_key"])
    if not chain or cur is None:
        return None
    chain.reverse()
    return chain


def _find_clickable(xml: str, package: str, label: str):
    """按到达动作文案找可点控件：全等 → 包含（explore 记录的是点击时的 label）。"""
    candidates = [n for n in parse_nodes(xml)
                  if n.clickable and n.in_package(package) and n.label]
    for node in candidates:
        if node.label == label:
            return node
    for node in candidates:
        if label in node.label:
            return node
    return None


def _foreground(drv) -> str:
    try:
        return drv.foreground_app().get("package") or ""
    except Exception:
        return ""


def _replay_once(drv, package: str, chain: list[dict], sleep_scale: float,
                 state: dict) -> tuple[bool, str]:
    """重放一轮。返回 (是否到达, 失败原因)；成功不返回原因。"""
    try:
        drv.app_start(package)
        time.sleep(2.0 * sleep_scale)
    except Exception as exc:
        return False, f"冷启动失败: {exc}"

    for i, hop in enumerate(chain):
        current = _foreground(drv)
        if current and current != package:
            return False, f"第 {i + 1} 跳离场（{current[:40]}）"
        try:
            xml = drv.dump()
        except Exception as exc:
            return False, f"第 {i + 1} 跳控件树获取失败: {exc}"
        node = _find_clickable(xml, package, hop["action"])
        if node is None:
            return False, f"第 {i + 1} 跳未找到文案「{hop['action']}」的可点控件"
        match = _BOUNDS_RE.match(node.bounds or "")
        if not match:
            return False, f"第 {i + 1} 跳控件 bounds 异常"
        x1, y1, x2, y2 = (int(g) for g in match.groups())
        try:
            drv.click((x1 + x2) // 2, (y1 + y2) // 2)
        except Exception as exc:
            return False, f"第 {i + 1} 跳点击失败: {exc}"
        state["clicks"] = state.get("clicks", 0) + 1
        time.sleep(1.5 * sleep_scale)
        try:
            landed = page_key(drv.dump(), package)
        except Exception as exc:
            return False, f"第 {i + 1} 跳落点确认失败: {exc}"
        if landed != hop["expected_key"]:
            return False, f"第 {i + 1} 跳落点不符（期望 {hop['expected_key'][:8]}，实到 {landed[:8]}）"
    return True, ""


def verify_arrivals(drv, package: str, index_path: Path, trials: int = 3,
                    max_paths: int = 8, click_budget: int = 60,
                    sleep_scale: float = 1.0, on_progress=None) -> dict:
    """试跑图谱中的到达路径并回写 `arrival` 字段。

    候选页：depth ≥ 1 且父链完整可重放；按深度升序（浅链先验，性价比高）。
    通过门槛：passed ≥ trials-1。
    """
    trials = max(2, trials)
    index_path = Path(index_path)
    index = load_index(index_path)
    if not index:
        raise FileNotFoundError(f"图谱不存在或不可读: {index_path}——请先 mat index / mat explore")

    # C1 同源修正（2026-10-12 评审）：唤醒走 BaseDriver 契约 `wake_and_unlock()`。
    # 此前 `raw is None ⇒ 跳过` 的判据在 raw 从真实驱动删除后恒成立，本模块的
    # 唤醒同样从未生效。
    try:
        drv.wake_and_unlock()
    except Exception:
        pass  # 桩未实现契约（单测）/真机唤醒失败，都不阻塞校验

    pages_by_key = {p.get("page_key"): p for p in index.get("pages", [])
                    if isinstance(p, dict) and p.get("page_key")}
    targets = [p for p in pages_by_key.values()
               if isinstance(p.get("depth"), int) and p["depth"] >= 1]
    targets.sort(key=lambda p: (p["depth"], p.get("first_seen") or ""))

    results: list[dict] = []
    clicks = 0
    verified_count = 0
    stopped_early = False
    for page in targets:
        if verified_count >= max_paths:
            break
        if clicks >= click_budget:
            stopped_early = True
            break
        chain = _chain_of(page, pages_by_key)
        if not chain or len(chain) > _MAX_PATH_LEN:
            results.append({"page_key": page["page_key"], "depth": page["depth"],
                            "status": "skipped", "reason": "父链不完整" if not chain
                            else f"链长 {len(chain)} 跳超过 {_MAX_PATH_LEN} 上限"})
            continue

        passed = 0
        rounds_done = 0
        last_reason = ""
        for _ in range(trials):
            if clicks >= click_budget:
                stopped_early = True
                break
            state = {"clicks": clicks}
            ok, reason = _replay_once(drv, package, chain, sleep_scale, state)
            clicks = state.get("clicks", clicks)
            rounds_done += 1
            if ok:
                passed += 1
            else:
                last_reason = reason

        entry = {
            "rounds": rounds_done,
            "passed": passed,
            "success_rate": round(passed / rounds_done, 3) if rounds_done else 0.0,
            "status": ("stable" if passed >= trials - 1 else "unstable") if rounds_done else "unverified",
            "reason": "" if not rounds_done else (
                f"试跑 {rounds_done} 轮通过 {passed} 轮，"
                + ("达到" if passed >= trials - 1 else "未达") + f"「通过 ≥{trials - 1}」门槛"
                + (f"；末次: {last_reason}" if last_reason else "")),
            "entry": [{"action": "launch", "target": "App冷启动"}]
            + [{"action": "tap", "target": hop["action"]} for hop in chain],
            "last_verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _update_page_field(index, page["page_key"], "arrival", entry)
        results.append({"page_key": page["page_key"], "depth": page["depth"],
                        "rounds": rounds_done, "passed": passed,
                        "status": entry["status"], "reason": entry["reason"]})
        verified_count += 1
        if on_progress:
            on_progress(verified_count, max_paths)

    save_index(index_path, index)
    return {"package": package, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "trials": trials, "clicks": clicks, "stopped_early": stopped_early,
            "verified": sum(1 for r in results if r.get("status") in ("stable", "unstable")),
            "stable": sum(1 for r in results if r.get("status") == "stable"),
            "results": results}


def _update_page_field(index: dict, page_key: str, field: str, value) -> None:
    """回写单页字段（复用 update_page 语义：不累加 hits、不碰 structure）。"""
    for page in index.get("pages", []):
        if isinstance(page, dict) and page.get("page_key") == page_key:
            page[field] = value
            break
    index["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def save_report(report: dict, artifacts_dir: Path) -> Path:
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out = artifacts_dir / "arrival.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
