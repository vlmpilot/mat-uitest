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


def bearer() -> str:
    """云端凭证，按**身份优先级**取：

    1. 本机授权换来的 token（`license_server.json`，团队授权 activate 时写入）；
    2. 匿名/邀请 token（`invite.json`）或 `GEN_API_KEY`。

    为什么授权优先：团队客户的身份是"这份合同"，不是某个匿名 ID。同一台机器可能先
    体验过、后来买了团队授权，以授权为准才符合直觉。标准版客户走不到团队命令
    （`entitlements` 先拦），顺序变化对他们没有影响。
    """
    from . import licensing
    from .invite import auth_token

    return licensing.read_server_token() or auth_token()


def _bearer() -> str:
    token = bearer()
    if not token:
        raise SystemExit(
            "缺少云端凭据：团队授权请先 `mat activate --file <license.json>`"
            "（会换发团队凭证）；没有授权时再用 `mat token` 换一张匿名凭证。")
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
        case_id = str(doc.get("caseId") or "").strip()
        if not case_id:
            print(f"  ⚠️ 跳过 {path.name}:没有 caseId——团队库主键是对账口径,缺失无法安全推送")
            skipped += 1
            continue
        try:
            out = cloudapi.request("POST", "/team/cases",
                                   {"app": pkg, "case_id": case_id, "name": name,
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


def _local_case_id(dest: Path) -> str | None:
    """读本地文件的 caseId：`""` = 文件不存在，`None` = 存在但读不出来（当成"自己的"）。

    为什么把读不出来当自己的：用户可能手改过、或文件是旧格式（无 caseId）。这种文件
    默认**不覆盖**（跳过），要覆盖得显式 `--force`——与迁移前行为一致，不突然改写用户文件。
    """
    if not dest.exists():
        return ""
    try:
        return str(json.loads(dest.read_text(encoding="utf-8")).get("caseId") or "")
    except (OSError, ValueError):
        return None


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
    used: dict[str, str] = {}  # 本批已占用的文件名 → case_id
    for item in meta:
        pkg, name, case_id = item["app"], item["name"], item["case_id"]
        try:
            detail = cloudapi.request(
                "GET", f"/team/cases/{pkg}/{case_id}", bearer=token)
        except cloudapi.CloudError as exc:
            print(f"  ❌ {pkg} · {_brief(name)}：{exc}")
            failed += 1
            continue
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name)[:60] or "case"
        dest = out_dir / f"{safe}.json"
        # 同名不同 caseId 不能共用一个文件：团队库现在按 caseId 存，两条同名用例
        # 都该落地（2026-10-30 实测：按文件名拉取时第二条被判「本地已有」直接丢掉）。
        owner = _local_case_id(dest)
        if (owner and owner != case_id) or used.get(dest.name, case_id) != case_id:
            dest = out_dir / f"{safe}__{case_id}.json"
        if dest.exists() and not force:
            print(f"  ⏭️ 本地已有 {dest.name}（--force 覆盖）")
            skipped += 1
            continue
        dest.write_text(detail["content"], encoding="utf-8")
        used[dest.name] = case_id
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


# -- 现状 -------------------------------------------------------------------

def status() -> int:
    """`mat team status`：本机授权 + 云端身份 + 团队库概览。

    为什么需要它：团队库的语义是"多台机器同一个库"，而这件事**从本机看不出来**。
    没有这条命令，客户只能靠"我推了他能不能拉到"来猜，接错了团队（最常见的是每台
    机器各自激活成独立团队）也不会报错。三件事必须一眼可见：我是什么档位、我和谁
    一个库、库里有什么。
    """
    from . import licensing

    ent = licensing.read_license()
    plan = str(ent.get("plan") or "free")
    seats = ent.get("seats")
    expires = ent.get("expires_at")
    bits = [licensing.plan_label(plan)]
    if seats:
        bits.append(f"{seats} 席")
    if expires:
        import time as _t

        bits.append("至 " + _t.strftime("%Y-%m-%d", _t.localtime(int(expires))))
    if ent.get("license_id"):
        bits.append(str(ent["license_id"]))
    print("本机授权: " + " · ".join(bits))

    if "team_share" not in (ent.get("features") or ()):
        print("云端身份: —（当前档位不含团队协作，服务端会拒绝这类请求）")
        return 1

    token = bearer()
    if not token:
        print("云端身份: 未连接（本机没有云端凭证）")
        print("  团队授权：mat activate --file <license.json>   # 会换发团队凭证")
        print("  无授权时：mat token --new")
        return 1
    try:
        usage = cloudapi.get("/usage", bearer=token)
        meta = cloudapi.get("/team/cases", bearer=token)
    except cloudapi.CloudError as exc:
        print(f"云端身份: 未连通（{exc}）")
        print("  本地功能不受影响；联网后重跑本命令即可。")
        return 1

    team = str(usage.get("team_id") or "")
    gen = usage.get("gen") or {}
    print(f"云端身份: 团队 {team or '未归属（服务端未返回 team_id，可能需要升级）'}"
          f" · 档位 {usage.get('plan')}"
          f" · 本月生成 {gen.get('used', 0)}/{gen.get('limit', '?')}")

    cases = meta.get("cases") or []
    apps = sorted({str(c.get("app")) for c in cases if c.get("app")})
    tail = f"（{'、'.join(apps[:5])}{' 等' if len(apps) > 5 else ''}）" if apps else ""
    print(f"团队库: {len(cases)} 条用例 · {len(apps)} 个应用{tail}")
    if cases:
        newest = max(cases, key=lambda c: str(c.get("updated_at") or ""))
        print(f"  最近更新: {_brief(str(newest.get('name') or ''))}"
              f" · {newest.get('actor')} · {str(newest.get('updated_at'))[:19]}")
    else:
        print("  库里还是空的：mat team push 把本地 cases/ 推上去。")
    return 0
