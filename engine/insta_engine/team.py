"""团队协作客户端：共享用例库 / 云端报告分享 / 审计（Team/Private）。

为什么客户端也判一次档位：服务端已经会拒（403），但**先判能给出可行动的提示**——
用户看到"当前档位不含团队协作能力"比看到一个 403 JSON 清楚得多。门控本身仍在服务端，
这里只是体验层（与 `entitlements` 的定位一致）。

命令只做三件事：读本地文件 → 调云 → 打印人话。冲突策略刻意简单：
用例按 `app+name` 覆盖（团队库要"最新一版"），`pull` 默认不覆盖本地已有文件。
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from . import cloudapi, entitlements


def _bearer() -> str:
    from .invite import auth_token

    token = auth_token()
    if not token:
        raise SystemExit(
            "缺少云端凭据：先 `mat token`（或把官方代理的 Key 配成 GEN_API_KEY）后再试。")
    return token


def _require(feature: str) -> None:
    try:
        entitlements.require(feature)
    except entitlements.EntitlementError as exc:
        raise SystemExit(f"❌ {exc}") from exc


def _case_files(root: Path) -> list[Path]:
    cases = root / "cases"
    if not cases.is_dir():
        return []
    return sorted(p for p in cases.glob("*.json") if not p.name.endswith(".suite.json"))


def _case_app(doc: dict) -> str:
    target = doc.get("target") or {}
    return str(target.get("package") or "").strip()


def _brief(name: str, limit: int = 46) -> str:
    text = (name or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- 共享用例库 ---------------------------------------------------------------

def push(root: Path, app: str | None = None) -> int:
    """把本地用例推上团队库（同 app+name 覆盖）。返回失败条数。"""
    _require("team_share")
    token = _bearer()
    files = _case_files(root)
    if not files:
        print("本地没有用例（cases/*.json）——先 `mat gen` 生成，或 `mat team pull` 拉团队的。")
        return 1
    ok = skipped = failed = 0
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  ⚠️ 跳过 {path.name}：不是合法 JSON（{exc}）")
            skipped += 1
            continue
        pkg = _case_app(doc)
        if not pkg:
            print(f"  ⚠️ 跳过 {path.name}：没有 target.package")
            skipped += 1
            continue
        if app and pkg != app:
            continue
        name = str(doc.get("name") or path.stem)
        try:
            out = cloudapi.request("POST", "/team/cases",
                                   {"app": pkg, "name": name,
                                    "content": json.dumps(doc, ensure_ascii=False)},
                                   bearer=token)
        except cloudapi.CloudError as exc:
            print(f"  ❌ {pkg} · {_brief(name)}：{exc}")
            failed += 1
            continue
        ok += 1
        print(f"  ✅ {pkg} · {_brief(name)} → {out.get('updated_at', '')}")
    print(f"\n推送完成：{ok} 条成功，{skipped} 条跳过，{failed} 条失败。")
    return 0 if failed == 0 else 1


def pull(root: Path, app: str | None = None, force: bool = False) -> int:
    """把团队库拉到本地 `cases/`；默认不覆盖本地同名文件。"""
    _require("team_share")
    token = _bearer()
    query = f"?app={app}" if app else ""
    meta = cloudapi.request("GET", f"/team/cases{query}", bearer=token).get("cases") or []
    if not meta:
        print("团队库是空的（或该应用下还没人推过）。")
        return 0
    out_dir = root / "cases"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = failed = 0
    for item in meta:
        pkg, name = item["app"], item["name"]
        try:
            detail = cloudapi.request(
                "GET", f"/team/cases/{pkg}/{name}", bearer=token)
        except cloudapi.CloudError as exc:
            print(f"  ❌ {pkg} · {_brief(name)}：{exc}")
            failed += 1
            continue
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name)[:60] or "case"
        dest = out_dir / f"{safe}.json"
        if dest.exists() and not force:
            print(f"  ⏭️ 本地已有 {dest.name}（--force 覆盖）")
            skipped += 1
            continue
        dest.write_text(detail["content"], encoding="utf-8")
        written += 1
        print(f"  ✅ {pkg} · {_brief(name)} → cases/{dest.name}")
    print(f"\n拉取完成：{written} 条写入，{skipped} 条跳过，{failed} 条失败。")
    return 0 if failed == 0 else 1


# -- 云端报告 ---------------------------------------------------------------

def _report_html(run: dict) -> str:
    """把 run.json 摊成一张能看的表（不引外部资源，分享页自包含）。"""
    case = run.get("case") or {}
    result = run.get("run") or {}
    name = result.get("case_name") or case.get("name") or "执行报告"
    status = result.get("status") or "unknown"
    rows = []
    for step in result.get("steps") or []:
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(str(step.get("step_id") or "")),
                escape(str(step.get("action") or "")),
                escape(str(step.get("status") or "")),
                escape(str(step.get("error") or ""))[:300]))
    table = ("<table><thead><tr><th>步骤</th><th>动作</th><th>状态</th><th>错误</th>"
             "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>") if rows else "<p>无步骤明细。</p>"
    head = (f"<p><b>用例</b>：{escape(str(name))}　<b>状态</b>：{escape(status)}　"
            f"<b>耗时</b>：{result.get('duration_ms', '?')} ms</p>")
    return head + table


def share_report(run_json: Path, title: str | None = None, ttl_days: int | None = None) -> int:
    _require("report_share")
    token = _bearer()
    if not run_json.is_file():
        print(f"找不到报告：{run_json}（指向 run.json 或含它的目录）")
        return 1
    try:
        run = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"报告不是合法 JSON：{exc}")
        return 1
    if title is None:
        result = run.get("run") or {}
        title = str(result.get("case_name") or run.get("case", {}).get("name") or run_json.parent.name)
    payload = {"title": title, "html": _report_html(run)}
    if ttl_days:
        payload["ttl_days"] = ttl_days
    try:
        out = cloudapi.request("POST", "/report/share", payload, bearer=token)
    except cloudapi.CloudError as exc:
        print(f"❌ 分享失败：{exc}")
        return 1
    url = f"{cloudapi.cloud_url()}{out['path']}"
    print(f"✅ 已生成分享链接（{out['days']} 天后过期）：\n   {url}")
    print("   链接本身就是凭据——拿到的人都能看，转发请注意范围。")
    return 0


# -- 审计 -------------------------------------------------------------------

def audit(limit: int = 50, action: str = "") -> int:
    _require("audit_log")
    token = _bearer()
    query = f"?limit={int(limit)}"
    if action:
        query += f"&action={action}"
    events = cloudapi.request("GET", f"/audit{query}", bearer=token).get("events") or []
    if not events:
        print("还没有审计记录。")
        return 0
    print(f"{'时间':20s} {'操作者':16s} {'动作':14s} {'对象':28s}")
    for e in events:
        print(f"{str(e['created_at'])[:19]:20s} {str(e['actor'])[:16]:16s} "
              f"{str(e['action'])[:14]:14s} {str(e['target'])[:28]:28s}")
    print(f"\n共 {len(events)} 条（新到旧）。")
    return 0
