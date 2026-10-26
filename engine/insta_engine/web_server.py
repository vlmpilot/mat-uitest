"""本地配置台服务（仅本机监听 127.0.0.1）——HTTP 层。

Phase 0 分层：全部 `api_*` 处理函数按域拆到 `web_api/` 包，本模块只保留
HTTP 层（Handler/路由表/静态文件/serve）。本模块**逐名重导出** web_api 的符号——
tests 与 mcp_server 的既有导入面（`ws.api_*`、`web_server._JOBS` 等）保持不变。

零第三方依赖（标准库 http.server）。P6 双入口同构：配置台与 CLI 操作同一份
cases/ artifacts/，无第二真相源。
安全边界：仅绑定 127.0.0.1；/artifacts 读取限制在 artifacts 目录内；用例落盘前过 DSL 校验。
`--share`（0.0.0.0）时要求访问口令（cookie 或 X-MAT-Key 头）：/api/* 与产物路径全拦，
SPA 壳放行（壳里无敏感数据）——口令由 cmd_console 生成/指定并打印在终端。
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .device import list_adb_devices  # noqa: F401 —— tests 经 web_server.list_adb_devices 引用
from .web_api.apps import (
    api_app_labels,
    api_app_note_get,
    api_app_note_put,
    api_app_probe,
    api_apps_install,
    api_apps_installed,
    api_apps_uninstall,
)
from .web_api.audit import api_audit
from .web_api.calibrate import api_calibrate_apply, api_calibrate_data, api_explore_calibrate
from .web_api.cases import (
    api_case_delete,
    api_case_get,
    api_case_rename,
    api_case_save,
    api_controls,
    api_import_convert,
    api_import_preview,
    api_suite_delete,
    api_suite_get,
    api_suite_save,
    api_suites_list,
)
from .web_api.cred import api_cred_list, api_cred_write
from .web_api.describe import api_describe
from .web_api.device import (
    api_device_key,
    api_device_scrcpy,
    api_devices,
    api_elements,
    api_ios_sim_boot,
    api_ios_simulators,
    api_screenshot,
    api_screenshot_capture,
)
from .web_api.diagnose import api_diagnose
from .web_api.export import api_export
from .web_api.heals import api_heal_apply, api_heal_list
from .web_api.indexing import api_index
from .web_api.invite import api_invite_action, api_invite_status
from .web_api.jobs import api_job_cancel, api_job_events, api_job_view, api_jobs_list
from .web_api.live import render_live
from .web_api.polish import api_polish
from .web_api.prompts import (
    api_prompts,
    api_prompts_content,
    api_prompts_save,
    api_prompts_select,
    api_prompts_upgrade,
)
from .web_api.readiness import api_readiness
from .web_api.review import api_review_list, api_review_save
from .web_api.runs import api_explore, api_gen, api_run
from .web_api.runtime import (  # noqa: F401
    _JOB_LOCK,
    _JOBS,
    CONSOLE_DIST,
    JOB_TIMEOUT_SECONDS,
    WEB_DIR,
    _require_fresh_code,
    device_lock,
    get_driver,
    invalidate_driver,
    safe_join,
    source_staleness,
    start_job,
    sweep_stale_jobs,
    watchdog_loop,
)
from .web_api.seats import api_seat_check, api_seat_remove, api_seats
from .web_api.seats import enforce as seat_enforce
from .web_api.share import api_share
from .web_api.state import api_activate, api_doctor, api_doctor_fix, api_state  # noqa: F401
from .web_api.version_check import api_version_check, start_background_check
from .web_api.workspace import (  # noqa: F401
    api_fs_browse,
    api_fs_open,
    api_workspace_switch,
    case_meta,
    list_explores,
    list_imports,
    list_indexes,
    list_runs,
)

#: 请求体上限（字节）。超过就回 **413**。
MAX_BODY_BYTES = 1_000_000

#: `--share` 口令鉴权用的 cookie 名（团队协作 P1，发起人 2026-10-12）。
AUTH_COOKIE = "mat_key"

#: 未通过口令时给浏览器的极简门槛页：输一次口令 → POST /api/auth → 种 cookie → 重载。
#: 只在 `/artifacts/` 的 HTML 请求（报告直达链接）上返回；SPA 壳本身放行，
#: 鉴权收敛在 /api/* 与产物路径（壳里没有敏感数据，API 才有）。
_GATE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>MAT 控制台 - 访问口令</title>
<style>body{font-family:system-ui;display:flex;justify-content:center;align-items:center;
height:100vh;margin:0;background:#f5f6fa}div{background:#fff;padding:32px;border-radius:12px;
box-shadow:0 2px 8px rgba(0,0,0,.08);width:320px;text-align:center}
input{width:100%;box-sizing:border-box;padding:8px 12px;margin:16px 0;border:1px solid #d9dce3;
border-radius:6px;font-size:14px}button{width:100%;padding:9px;border:0;border-radius:6px;
background:#4F5BFF;color:#fff;font-size:14px;cursor:pointer}p{color:#8a919f;font-size:13px;margin:0 0 12px}
#err{color:#cf1322;font-size:12px;min-height:16px}</style></head><body><div>
<p>这台服务开启了局域网分享，需要访问口令<br>（向跑 MAT 的同事要）</p>
<input id="k" type="password" placeholder="访问口令" autofocus>
<div id="err"></div><button onclick="go()">进入</button>
<script>function go(){fetch('/api/auth',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({key:document.getElementById('k').value})})
.then(r=>{if(r.ok)location.reload();else document.getElementById('err').textContent='口令不对，再试一次'})}
document.getElementById('k').addEventListener('keydown',e=>{if(e.key==='Enter')go()})</script></div></body></html>""".encode()

#: 回 413 之前**最多读掉多少**请求体（有界）。
#: 为什么需要读：见 `do_POST` 里的注释——不读会让客户端**写 body 时吃到 RST**。
#: 为什么有界：超大 body（恶意/误配）不该把服务器拖死；超过这个量就读不完，
#: 客户端可能仍看到连接重置 —— **这是刻意的取舍**（对"远超上限"的请求，
#: 干净地拒绝不如不被打死重要）。
_DRAIN_LIMIT = 16_000_000


class ConsoleHandler(BaseHTTPRequestHandler):
    # /api/auth 爆破防护（2026-10-20 评审 P1-4）：按来源 IP 滑动窗口计失败次数，
    # 超限进入冷却；成功清零。口令是「跑用例/删改用例/烧额度」的全权钥匙，
    # 此前无计数、无冷却、失败不落审计——内网纯脚本爆破零成本。
    _AUTH_FAILS: dict[str, list[float]] = {}  # ip -> 窗口内失败时间戳（monotonic）
    _AUTH_FAIL_LOCK = threading.Lock()
    _AUTH_WINDOW_S = 300.0    # 滑动窗口
    _AUTH_MAX_FAILS = 5       # 窗口内失败上限
    _AUTH_COOLDOWN_S = 30.0   # 超限后冷却时长

    #: 工作区根：优先 server 实例的 ws_root（/api/workspace/switch 在线切换），
    #: 回退 make_server 绑定的类属性。V2 之前是固定类属性——切换要重启进程。
    @property
    def root(self) -> Path:
        return getattr(self.server, "ws_root", None) or Path.cwd()

    def log_message(self, *args):  # 静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path: Path, ctype: str) -> None:
        if not path.exists():
            return self._send(404, b"missing", "text/plain")
        self._send(200, path.read_bytes(), ctype)

    # -- GET：纯 JSON 端点路由表（特殊形态——bytes 静态/文件服务——留在 do_GET 分支） --
    def _get_json_routes(self):
        return {
            "/api/state": lambda: api_state(self.root),
            "/api/version/check": lambda: (200, api_version_check()),
            "/api/devices": lambda: api_devices(),
            "/api/elements": lambda: api_elements(
                (self.query.get("serial") or [None])[0],
                (self.query.get("platform") or ["android"])[0]),
            "/api/doctor": lambda: api_doctor((self.query.get("platform") or ["all"])[0], self.root),
            "/api/ios/simulators": lambda: api_ios_simulators(),
            "/api/readiness": lambda: api_readiness(
                self.root, (self.query.get("package") or [""])[0]),
            "/api/review": lambda: api_review_list(self.root, (self.query.get("package") or [""])[0]),
            "/api/controls": lambda: api_controls(self.root, (self.query.get("package") or [""])[0]),
            "/api/app/labels": lambda: api_app_labels(
                self.root, (self.query.get("packages") or [""])[0]),
            # 按应用的测试说明（黑/白名单等，gen/polish 自动注入 prompt）
            "/api/app/note": lambda: api_app_note_get(
                self.root, (self.query.get("package") or [""])[0]),
            "/api/app/probe": lambda: api_app_probe(
                (self.query.get("path") or [""])[0], str(self.client_address[0])),
            # 已安装第三方 App（遍历探索包名候选；2026-10-22）
            "/api/apps/installed": lambda: api_apps_installed(
                (self.query.get("serial") or [""])[0]),
            "/api/jobs": lambda: api_jobs_list(),
            # 邀请（main 的 `/api/invite` 迁入 `web_api/invite.py`；**只读本地不联网**）
            "/api/invite": lambda: api_invite_status(),
            # `/api/device/elements` 是 main 侧的旧名（本分支已改名 `/api/elements`）——
            # 两个都注册，免得 main 侧的调用方 404（同一处理器，零风险）
            "/api/device/elements": lambda: api_elements(
                (self.query.get("serial") or [None])[0],
                (self.query.get("platform") or ["android"])[0]),
            "/api/seats": lambda: api_seats(self.root, self._shared()),
            "/api/prompts": lambda: api_prompts(self.root),
            "/api/prompts/content": lambda: api_prompts_content(
                self.root, (self.query.get("file") or [""])[0]),
            "/api/audit": lambda: api_audit(self.root,
                                            (self.query.get("limit") or ["50"])[0]),
            "/api/job/events": lambda: api_job_events(
                {"id": (self.query.get("id") or [""])[0],
                 "since": (self.query.get("since") or ["0"])[0]}),
            "/api/job": lambda: api_job_view((self.query.get("id") or [""])[0]),
        }

    def _auth_cooldown_remaining(self, ip: str) -> float:
        """该来源 IP 的剩余冷却秒数（0=未限流）。滑动窗口内失败 ≥5 次 ⇒ 冷却 30s。"""
        now = time.monotonic()
        with self._AUTH_FAIL_LOCK:
            fails = [t for t in self._AUTH_FAILS.get(ip, [])
                     if now - t < self._AUTH_WINDOW_S]
            self._AUTH_FAILS[ip] = fails
            if len(fails) < self._AUTH_MAX_FAILS:
                return 0.0
            return max(0.0, self._AUTH_COOLDOWN_S - (now - fails[-1]))

    def _auth_record_fail(self, ip: str) -> None:
        with self._AUTH_FAIL_LOCK:
            self._AUTH_FAILS.setdefault(ip, []).append(time.monotonic())

    def _auth_reset_fails(self, ip: str) -> None:
        with self._AUTH_FAIL_LOCK:
            self._AUTH_FAILS.pop(ip, None)

    def _auth_key(self) -> str | None:
        """本 server 实例的访问口令（None=未启用鉴权，一切放行）。"""
        return getattr(self.server, "auth_key", None)

    def _shared(self) -> bool:
        """当前是不是**共享模式**（`--share`，监听 0.0.0.0）。

        成员名额只在共享模式下有意义：本机监听的默认控制台，物理上只有安装者能打开，
        上限恒为 1（见 `web_api.seats`）；而共享之后给几个人，还要看**授权开不开放
        共享**（2026-10-25：仅团队授权，见 `seat_quota.team_authorized`）。
        这里按**实际监听地址**判断而不是看 `--share` 参数——测试与嵌入场景直接
        `make_server(host=...)` 也一视同仁。
        """
        return self.server.server_address[0] not in ("127.0.0.1", "localhost", "::1")

    def _authed(self) -> bool:
        """`--share` 口令鉴权（团队协作 P1）：cookie（浏览器一次输入长期有效）
        或 `X-MAT-Key` 头（curl/脚本）任一匹配即可。未配置口令恒 True。
        比对一律 compare_digest（2026-10-20 评审 P3：此前此处用 `==`）。"""
        key = self._auth_key()
        if key is None:
            return True
        if hmac.compare_digest(self.headers.get("X-MAT-Key") or "", key):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == AUTH_COOKIE and hmac.compare_digest(v, key):
                return True
        return False

    def _auth_gate_or_none(self, path: str):
        """GET 的鉴权关卡。需要挡下时返回响应动作（None=放行）：
        - /api/*：401 JSON（SPA 里 api.ts 收到后弹口令 Modal）；
        - /live（实时视图页）：门槛页——页面 HTML 内嵌 serial/job 元数据
          （2026-10-25 评审 P1 补：此前不在白名单 ⇒ --share 下未认证可读）；
        - /artifacts//screenshots/ 的 HTML 请求（报告直达链接）：门槛页；
        - 其余产物请求（图片/视频/zip）：401（浏览器已带 cookie 的正常场景不受影响）。
        SPA 壳（与 /assets/）不拦——里面没有敏感数据，鉴权收敛在 API 与产物。"""
        if self._authed():
            return None
        if path.startswith("/api/"):
            return lambda: self._json(401, {"error": "需要访问口令", "auth": True})
        if path == "/live" or path.startswith("/live/"):
            return lambda: self._send(401, _GATE_HTML, "text/html; charset=utf-8")
        if (path.startswith("/artifacts/") or path.startswith("/screenshots/")) \
                and "text/html" in (self.headers.get("Accept") or ""):
            return lambda: self._send(401, _GATE_HTML, "text/html; charset=utf-8")
        if path.startswith(("/artifacts/", "/screenshots/")):
            return lambda: self._json(401, {"error": "需要访问口令", "auth": True})
        return None

    def do_GET(self) -> None:
        if not self._host_allowed():
            return self._json(403, {"error": "host rejected"})
        parsed = urlparse(self.path)
        # 浏览器对路径里的非 ASCII（产物目录名含中文）自动 percent-encode，
        # 不 unquote 则拿编码后的字面路径找文件必 404（发起人 2026-10-12 实测踩中：
        # 报告页「报告↗」对导入用例产物全挂）。query 走 parse_qs 自带解码不受影响。
        path, query = unquote(parsed.path), parse_qs(parsed.query)
        gate = self._auth_gate_or_none(path)
        if gate is not None:
            return gate()
        try:
            if path in ("/", "/index.html"):
                if (CONSOLE_DIST / "index.html").exists():
                    return self._file(CONSOLE_DIST / "index.html", "text/html; charset=utf-8")
                return self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            if path == "/static/app.js":
                return self._file(WEB_DIR / "app.js", "text/javascript; charset=utf-8")
            if path == "/static/style.css":
                return self._file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            if path.startswith("/assets/") and (CONSOLE_DIST / "index.html").exists():
                target = safe_join(CONSOLE_DIST, path.lstrip("/"))
                if target is not None and target.exists():
                    ctype = ("application/javascript; charset=utf-8" if target.suffix == ".js"
                             else "text/css; charset=utf-8" if target.suffix == ".css"
                             else "image/svg+xml" if target.suffix == ".svg"
                             else "application/octet-stream")
                    return self._send(200, target.read_bytes(), ctype)
                return self._send(404, b"not found", "text/plain")
            # 使用手册（发起人 2026-10-13）：console/public/manual.html 随构建进 dist，
            # 侧栏左下角「使用手册」链接指向这里。SPA 兜底(下方)会把一切路径回落
            # index.html，静态文件必须在此之前显式接管。
            if path in ("/manual", "/manual.html") and (CONSOLE_DIST / "manual.html").exists():
                return self._file(CONSOLE_DIST / "manual.html", "text/html; charset=utf-8")
            route = self._get_json_routes().get(path)
            if route is not None:
                self.query = query
                return self._json(*route())
            if path == "/api/screenshot":
                serial = (query.get("serial") or [None])[0]
                platform = (query.get("platform") or ["android"])[0]
                raw_w = (query.get("width") or query.get("w") or [None])[0]
                width = int(raw_w) if raw_w else None
                code, data, ctype = api_screenshot(serial, width, platform)
                return self._send(code, data, ctype)
            if path == "/api/cred":
                # 本机能力门控按【客户端来源 IP】判定（client_address 是 TCP 层事实，
                # 不可伪造）；此前误传 server_address（--share 下恒 0.0.0.0）导致
                # 分享模式连本机也被 403（2026-10-15 修复）。
                return self._json(*api_cred_list(str(self.client_address[0])))
            if path == "/api/fs":
                return self._json(*api_fs_browse(str(self.client_address[0]),
                                                 (query.get("path") or [""])[0],
                                                 (query.get("ext") or [None])[0]))
            if path == "/api/share":
                host, port = self.server.server_address[:2]
                return self._json(*api_share(str(host), int(port)))
            if path == "/api/calibrate":
                return self._json(*api_calibrate_data(self.root, (query.get("run") or [""])[0]))
            if path == "/api/heals":
                return self._json(*api_heal_list(self.root))
            if path == "/api/suites":
                return self._json(*api_suites_list(self.root))
            if path == "/api/suite":
                name = (query.get("name") or [""])[0]
                return self._json(*api_suite_get(self.root, name))
            if path == "/api/case":
                # 用例原文件字节服务（不做 JSON 重序列化，保持与落盘逐字节一致）
                code, data, ctype = api_case_get(self.root, (query.get("name") or [""])[0])
                return self._send(code, data, ctype)
            if path in ("/api/explore", "/api/import"):
                # 批次产物文件服务（safe_join 防越界）
                import_id = (query.get("id") or [""])[0]
                if not import_id.isalnum() or ".." in import_id:
                    return self._json(400, {"error": "非法批次 id"})
                name = "explore.json" if path == "/api/explore" else "import.json"
                target = safe_join(self.root / "artifacts" / path.rsplit("/", 1)[1],
                                   f"{import_id}/{name}")
                if target is None or not target.exists():
                    return self._json(404, {"error": "not found"})
                return self._send(200, target.read_bytes(), "application/json; charset=utf-8")
            if path.startswith("/screenshots/"):
                # 人工截屏的固定目录（与 artifacts/ 平级的独立存储，发起人需求）
                base = (self.root / "screenshots").resolve()
                target = safe_join(base, path[len("/screenshots/"):])
                if target is None or not target.exists():
                    return self._send(404, b"not found", "text/plain")
                ctype = "image/png" if target.suffix == ".png" else "text/plain; charset=utf-8"
                return self._send(200, target.read_bytes(), ctype)
            if path.startswith("/artifacts/"):
                base = (self.root / "artifacts").resolve()
                target = safe_join(base, path[len("/artifacts/"):])
                if target is None or not target.exists():
                    return self._send(404, b"not found", "text/plain")
                ctype = {
                    ".html": "text/html; charset=utf-8",
                    ".png": "image/png",
                    ".zip": "application/zip",
                    ".mp4": "video/mp4",           # 录屏（P3）随报告相对引用播放
                    ".json": "application/json; charset=utf-8",
                }.get(target.suffix, "text/plain; charset=utf-8")
                return self._send(200, target.read_bytes(), ctype)
            if path == "/live" or path.startswith("/live/"):
                # 任务实时视图页（只读旁路：设备投屏 + 事件流；门控口径见 web_api/live.py
                # ——必须在 SPA 兜底之前接管，否则 /live/* 会被 index.html 吞掉）
                job_id = path[len("/live/"):] if path.startswith("/live/") else ""
                code, html = render_live(job_id)
                return self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")
            if (CONSOLE_DIST / "index.html").exists() and not path.startswith(("/api/", "/artifacts/")):
                return self._file(CONSOLE_DIST / "index.html", "text/html; charset=utf-8")
            return self._json(404, {"error": "not found"})
        except Exception as exc:
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _host_allowed(self) -> bool:
        """A1 缓解（2026-10-12 评审）：校验 Host 头——防 DNS rebinding 的唯一有效手段。

        rebinding 攻击链：浏览器解析恶意域名 → 攻击页脚本把同一域名再次解析到
        127.0.0.1 → 请求打到本服务。这类请求**必带 Host: <恶意域名>**（浏览器按
        原始域名填），且对 GET 同样有效——do_GET 此前零校验，脚本可读
        /api/screenshot、/api/device/elements 与 /artifacts/*。
        规则：
        - 缺 Host（HTTP/1.0 类客户端）放行——rebinding 走浏览器必带 Host，宽松只
          影响本机脚本（它们本来就能直连，完整 token 方案另行处理）；
        - Host ∈ {localhost, 127.0.0.1}[:port] 放行；
        - `--share`（0.0.0.0 监听）沿用既有「无鉴权内网分享」语义，不校验。
        """
        server_host, port = self.server.server_address[:2]
        if server_host not in ("127.0.0.1", "localhost"):
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        hostname, _, hport = host.rpartition(":") if ":" in host else (host, "", "")
        return hostname in ("localhost", "127.0.0.1") and hport in ("", str(port))

    def _origin_allowed(self) -> bool:
        """S7 缓解：浏览器跨域写请求（CSRF）必带 Origin/Referer；本机同源放行。

        规则：
        - 不带 Origin/Referer 的客户端（curl/pytest）天然放行——CSRF 是浏览器行为；
        - `--share`（0.0.0.0 监听）维持既有「无鉴权内网分享」语义（启动时已有
          警示文案），不做 Origin 校验；
        - 其余情况只放行与本服务同源的 Origin（scheme + host:port 都要匹配）。
        """
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        host, port = self.server.server_address[:2]
        if host not in ("127.0.0.1", "localhost"):
            return True
        parts = origin.split("/")
        host_port = parts[2] if len(parts) > 2 else ""
        return parts[0] == "http:" and host_port in (f"127.0.0.1:{port}", f"localhost:{port}")

    # -- POST：全部端点统一 (root, body) 或 (body) → (code, dict)，单表分派 --
    def _post_routes(self) -> dict:
        return {
            "/api/review": lambda: api_review_save(self.root, self.body),
            "/api/case": lambda: api_case_save(self.root, self.body),
            "/api/case/delete": lambda: api_case_delete(self.root, self.body),
            "/api/case/rename": lambda: api_case_rename(self.root, self.body),
            "/api/gen": lambda: api_gen(self.root, self.body),
            "/api/polish": lambda: api_polish(self.root, self.body),
            "/api/import/preview": lambda: api_import_preview(self.root, self.body),
            "/api/import/convert": lambda: api_import_convert(self.root, self.body),
            "/api/explore": lambda: api_explore(self.root, self.body),
            "/api/run": lambda: api_run(self.root, self.body),
            "/api/job/cancel": lambda: api_job_cancel(self.body),
            # 邀请动作（**唯一会联网的地方**，且必须由用户点出来；见 web_api/invite.py）
            "/api/invite": lambda: api_invite_action(self.body),
            "/api/device/key": lambda: api_device_key(self.body),
            "/api/device/scrcpy": lambda: api_device_scrcpy(self.body),
            "/api/ios/sim/boot": lambda: api_ios_sim_boot(self.body),
            "/api/screenshots/capture": lambda: api_screenshot_capture(
                self.root, str(self.server.server_address[:2][0]), self.body),
            "/api/activate": lambda: api_activate(self.body),
            "/api/heals/apply": lambda: api_heal_apply(self.root, self.body),
            "/api/workspace/switch": lambda: api_workspace_switch(
                self.server, self.root, str(self.client_address[0]), self.body),
            "/api/fs/open": lambda: api_fs_open(
                str(self.client_address[0]), str(self.body.get("path") or self.root)),
            "/api/doctor/fix": lambda: api_doctor_fix(
                str(self.client_address[0]), self.root, self.body),
            "/api/index": lambda: api_index(self.root, self.body),
            "/api/suites/save": lambda: api_suite_save(self.root, self.body),
            "/api/suites/delete": lambda: api_suite_delete(self.root, self.body),
            "/api/apps/install": lambda: api_apps_install(
                self.root, str(self.client_address[0]), self.body),
            "/api/app/note": lambda: api_app_note_put(self.root, self.body),
            "/api/apps/uninstall": lambda: api_apps_uninstall(
                self.root, str(self.client_address[0]), self.body),
            "/api/calibrate/apply": lambda: api_calibrate_apply(
                self.root, str(self.client_address[0]), self.body),
            "/api/explore/calibrate": lambda: api_explore_calibrate(
                self.root, str(self.client_address[0]), self.body),
            "/api/describe": lambda: api_describe(self.root, self.body),
            "/api/diagnose": lambda: api_diagnose(self.root, self.body),
            "/api/export": lambda: api_export(self.root, self.body),
            "/api/cred": lambda: api_cred_write(
                str(self.client_address[0]), self.body),
            "/api/seats/check": lambda: api_seat_check(self.root, self.body, self._shared()),
            "/api/seats/remove": lambda: api_seat_remove(self.root, self.body, self._shared()),
            "/api/prompts/select": lambda: api_prompts_select(self.root, self.body),
            "/api/prompts/save": lambda: api_prompts_save(self.root, self.body),
            # 过期模板副本升级（2026-10-25：改名备份回落包内新版，追加段不受影响）
            "/api/prompts/upgrade": lambda: api_prompts_upgrade(self.root, self.body),
        }

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if not self._host_allowed():
                return self._json(403, {"error": "host rejected"})
            if not self._origin_allowed():
                return self._json(403, {"error": "cross-origin rejected"})
            if path == "/api/auth":
                # 口令校验（团队协作 P1）——唯一无需先过鉴权的写端点（它本身就是为了拿到鉴权）。
                # 爆破防护（2026-10-20 评审 P1-4）：窗口内失败 5 次 ⇒ 冷却 30s，
                # 失败落审计（此前失败零痕迹）；比对用 compare_digest。
                key = self._auth_key()
                ip = str(self.client_address[0])
                if key is not None:
                    cooldown = self._auth_cooldown_remaining(ip)
                    if cooldown > 0:
                        return self._json(429, {"error": f"口令错误次数过多，"
                                                f"请 {int(cooldown) + 1} 秒后重试",
                                                "auth": True})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                given = str(body.get("key") or "")
                if key is not None and hmac.compare_digest(given, key):
                    self._auth_reset_fails(ip)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Set-Cookie",
                                     f"{AUTH_COOKIE}={key}; Path=/; Max-Age=31536000;"
                                     " SameSite=Lax; HttpOnly")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if key is not None:
                    self._auth_record_fail(ip)
                    self.body = {}  # _audit 读 self.body；口令本体绝不落审计
                    self._audit("/api/auth:fail")
                return self._json(401, {"error": "口令不对", "auth": True})
            if not self._authed():
                return self._json(401, {"error": "需要访问口令", "auth": True})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                # ⚠️ **必须先读掉请求体（有界），再回 413**（2026-10-07 修 flaky）。
                #
                # 旧写法直接 `return self._json(413, ...)` —— 此时客户端**还在写**那 1MB+ 的
                # body，服务端却已经回响应并关连接 ⇒ 客户端写 socket 吃到 RST
                # ⇒ `requests` 抛 `ConnectionError`/`BrokenPipe`，而不是读到干净的 413。
                #
                # 为什么**时好时坏**：整段 body 能不能塞进 socket 缓冲（本机约 100KB–1MB）
                # 决定客户端是否会被打断。实测：全量门禁里挂 3 次（机器有负载）、单独跑却通过。
                # ⇒ 这是**产品的真问题**（浏览器/curl 也会看到连接重置），不只是测试问题。
                #
                # 读掉是**有界**的（`_DRAIN_LIMIT`）：超大 body 不该把服务器拖死。
                self.close_connection = True
                remaining = min(length, _DRAIN_LIMIT)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                return self._json(413, {"error": "body too large"})
            self.body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            route = self._post_routes().get(path)
            if route is not None:
                # 人数闸（2026-10-12 补洞）：写操作必须带已登记的执行人名字——匿名/未登记
                # 一律拒绝，堵死「先匿名绕过名额」。豁免登记入口自身与机器级激活。
                if path not in ("/api/seats/check", "/api/activate"):
                    gate = seat_enforce(self.root, self.body, self._shared())
                    if gate is not None:
                        return self._json(*gate)
                self._audit(path)  # 团队协作：所有写操作落审计（谁改的/谁下发的，发起人 2026-10-12）
                return self._json(*route())
            return self._json(404, {"error": "not found"})
        except Exception as exc:
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    _AUDIT_LOCK = threading.Lock()
    _AUDIT_KEYS = ("name", "case", "case_name", "suite", "id", "job_id", "package",
                   "serial", "path", "title", "files", "target")

    def _audit(self, path: str) -> None:
        """写操作审计（团队协作 P1）：POST 全记（除 /api/auth 已单独 return）。

        落工作区 `artifacts/audit.jsonl`——随工作区走、与报告同级（备份/迁移天然带上）。
        记录失败**绝不打断业务**（审计是旁观者，不是门卫）。"""
        try:
            body = getattr(self, "body", {}) or {}
            parts = [f"{k}={str(body[k])[:60]}" for k in self._AUDIT_KEYS if body.get(k)]
            row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                   "operator": str(body.get("operator") or ""),
                   "action": path, "target": "; ".join(parts)[:200],
                   "ip": self.client_address[0]}
            root = getattr(self.server, "ws_root", None)
            if root is None:
                return
            audit = Path(root) / "artifacts" / "audit.jsonl"
            audit.parent.mkdir(parents=True, exist_ok=True)
            with self._AUDIT_LOCK:
                with audit.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — 审计失败静默（见 docstring）
            pass


def lan_urls(port: int = 7420) -> list[str]:
    """局域网地址枚举（实现已迁 web_api.runtime；保留重导出兼容既有导入面）。"""
    from .web_api.runtime import lan_urls as _lan_urls
    return _lan_urls(port)


def make_server(port: int = 7420, root: Path | None = None,
                host: str = "127.0.0.1", auth_key: str | None = None) -> ThreadingHTTPServer:
    """构造控制台 HTTP 服务器（**不起线程、不阻塞**）——`serve()` 与测试共用。

    为什么单独抽出来（2026-10-07 修 flaky）：测试需要**能关掉**服务器。
    旧夹具只能拿到端口号、拿不到 server 对象 ⇒ **每个测试都留一个服务器线程 +
    一个 watchdog 线程不回收**（全量 ~40 个测试 ⇒ 80 个线程）⇒ 负载高的机器上
    响应被拖慢到超过客户端 `timeout`（实测那条 413 用例全量里挂 3 次、单独跑通过）。

    ⚠️ 传 `port=0` 让内核直接分配端口，**不要**"先 bind(0) 拿端口、close、再 serve"——
    那中间有一个窗口会让别人抢走同一个端口（实测循环跑时偶发 setup 失败）。

    `auth_key`：访问口令（团队协作 P1）——挂到 server 实例上（非模块全局），
    测试可并行起不同口令的实例互不污染。
    """
    handler = type("BoundConsoleHandler", (ConsoleHandler,), {})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.ws_root = Path(root).resolve() if root else Path.cwd()  # type: ignore[attr-defined]
    srv.auth_key = auth_key  # type: ignore[attr-defined]
    return srv


def serve(port: int = 7420, root: Path | None = None, host: str = "127.0.0.1",
          auth_key: str | None = None) -> None:
    import threading

    server = make_server(port, root, host, auth_key=auth_key)
    threading.Thread(target=watchdog_loop, daemon=True).start()
    start_background_check()  # 新版本检查：启动时后台查一次 PyPI，失败静默（不阻塞服务）

    # prompt 解析口径统一：进程内任务（生成/转换/润色）cwd 是引擎安装目录，
    # 不注入的话 promptstore 会去引擎目录下找 prompts/，与 --root 工作区分裂。
    os.environ["MAT_WORKSPACE"] = str(root)

    def _sync_seat_quota() -> None:
        """启动时后台同步一次成员/视觉兜底配额（seat_quota，2026-10-14）。

        断网/服务端异常一律静默——配额拉不到就用本地缓存/默认值，
        **不能因为同步失败把控制台挡在门外**（同版本检查的零打扰原则）。
        """
        try:
            from . import seat_quota
            seat_quota.sync_from_server()
        except Exception:
            pass

    threading.Thread(target=_sync_seat_quota, name="mat-seat-quota-sync",
                     daemon=True).start()
    stale = source_staleness()
    if stale["stale"]:
        print(f"[!] 注意：{stale['file']} 比本进程新 {stale['behind_seconds']}s，"
              "当前跑的是启动时加载的旧代码；改过引擎源码请重启本命令。", flush=True)
    print(f"console: http://127.0.0.1:{port}  (Ctrl-C 退出)", flush=True)
    server.serve_forever()
