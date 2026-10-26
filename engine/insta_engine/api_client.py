"""API 业务数据断言（P1-1，2026-10-19）。

**解决什么**：UI 断言只能验证「页面长什么样」，验证不了「业务对不对」——
下单后订单状态、支付结果、余额变动必须打接口核实。这是竞品分析里
「文档承诺、实现缺失、行业点名」三重撞点的第一缺口。

**形态**：`Action.API` 步骤，params 携带 method/url/headers/timeoutMs/expect/save。
**不需要设备**：runner 对 api 步骤提前返回，不 dump 控件树、不定位、不截图——
没有真机也能跑回归里的业务校验段。

设计边界（刻意收窄，防范围膨胀）：

- JSONPath 只支持 `$.a.b[0].c` 简单路径（点号+数字下标），不引入 jmespath 依赖；
- 只允许 http/https；`MAT_API_DENY_PRIVATE_NETWORK=1` 时拒绝内网地址；
- 鉴权头一律用 `${cred.*}` / `${var}` 引用——密钥只存在于 CredentialStore，
  用例文件、报告、run.json 均不落明文（响应头集合里含敏感名的键值打码）；
- **永不抛异常**：一切失败（配置错/网络错/断言败）都折进返回的 evidence dict，
  由 runner 据此置 failed + category="api"——契约与「证据缺失不改变执行结果」同纪律。
"""

from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlparse

import requests

#: 请求头回显时需要打码的键名（小写比较；含子串 token/key/secret 的也打码）
_REDACT_KEYS = {"authorization", "proxy-authorization", "cookie", "set-cookie",
                "x-api-key", "x-auth-token", "x-csrf-token"}
#: 请求体/响应体/变量提取的键名脱敏判据（2026-10-19 评审 P1-1）：
#: 凭据形态的键（含子串）一律不落明文——方案 §3.5.2「只存脱敏片段」的实现依据
_SENSITIVE_KEY = re.compile(
    r"passw|passwd|secret|token|authorization|cookie|credential|api[-_]?key", re.I)
#: 响应体进证据的最大字节数（截断，防大响应撑爆 run.json/报告）
_MAX_BODY_SNIPPET = 2048
#: 默认超时（毫秒）——params.timeoutMs 未给时用
_DEFAULT_TIMEOUT_MS = 10_000
_PRIVATE_HOST = re.compile(
    r"^(localhost$|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?$)")


def substitute_deep(obj, variables: dict[str, str]):
    """对 params 里的字符串递归做 ${var}/${cred.x} 替换；非字符串原样返回。"""
    from .runner import substitute_vars

    if isinstance(obj, str):
        return substitute_vars(obj, variables)
    if isinstance(obj, list):
        return [substitute_deep(v, variables) for v in obj]
    if isinstance(obj, dict):
        return {k: substitute_deep(v, variables) for k, v in obj.items()}
    return obj


def _mask(value: str, key: str) -> str:
    lk = key.lower()
    if lk in _REDACT_KEYS or "token" in lk or "secret" in lk or lk.endswith("key"):
        return "***"
    return value


def _redact_json(obj):
    """递归键名打码：凭据形态的键 → "***"，结构与非敏感值原样保留。

    只影响**证据展示**（run.json/报告）；断言与 save 仍读原始响应。
    """
    if isinstance(obj, dict):
        return {k: ("***" if isinstance(k, str) and _SENSITIVE_KEY.search(k)
                    else _redact_json(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_json(v) for v in obj]
    return obj


def _redact_url(url: str) -> str:
    """URL 脱敏（2026-10-20 评审 P1-5③）：query 参数里凭据形态的键打码。

    脱敏链此前的漏网环节：请求体/响应体/头都打码了，但 `?token=…` 这类
    拼在 URL 上的凭据（`${cred.*}` 替换后）原样落 run.json/报告。
    解析失败（畸形 URL）时保守兜底：整体返回 "***"，不给漏网机会。
    """
    from urllib.parse import parse_qsl, urlencode, urlunparse

    if "?" not in url:
        return url
    try:
        parts = urlparse(url)
        # urlencode 会把 *** 编成 %2A%2A%2A——证据是给人看的，换回来
        query = urlencode([(k, "***" if _SENSITIVE_KEY.search(k) else v)
                           for k, v in parse_qsl(parts.query, keep_blank_values=True)]
                          ).replace("%2A%2A%2A", "***")
        return urlunparse(parts._replace(query=query))
    except (ValueError, TypeError):
        return "***"


def _redact_text(text: str) -> str:
    """响应体文本脱敏：可解析为 JSON 走结构化打码；否则按「敏感键":"值"」正则兑底。"""
    try:
        return json.dumps(_redact_json(json.loads(text)), ensure_ascii=False)
    except (ValueError, TypeError):
        return re.sub(
            r'"([^"]*)"\s*:\s*"[^"]*"',
            lambda m: f'"{m.group(1)}": "***"' if _SENSITIVE_KEY.search(m.group(1))
            else m.group(0),
            text)


def _json_path(doc, path: str) -> tuple[bool, object]:
    """`$.a.b[0].c` 简单取值；路径走不通返回 (False, None)——与「值恰好是 null」区分靠 found。"""
    if not isinstance(path, str) or not path.startswith("$"):
        raise ValueError(f"jsonPath 必须以 $ 开头：{path!r}")
    cur = doc
    for m in re.finditer(r"\.([A-Za-z_][\w\-]*)|\[(\d+)\]", path[1:]):
        if m.group(1) is not None:
            if not isinstance(cur, dict) or m.group(1) not in cur:
                return False, None
            cur = cur[m.group(1)]
        else:
            idx = int(m.group(2))
            if not isinstance(cur, list) or idx >= len(cur):
                return False, None
            cur = cur[idx]
    return True, cur


def _check_expect(exp: dict, status: int | None, resp_headers: dict,
                  body_text: str) -> tuple[bool, str]:
    """单条 expect：返回 (passed, 人读结果)。未知写法按失败处理（fail closed）。"""
    if "status" in exp:
        want = int(exp["status"])
        return (status == want, f"期望状态 {want}，实际 {status}")
    if "header" in exp:
        name = str(exp["header"])
        actual = resp_headers.get(name.lower())
        if "equals" in exp:
            want = str(exp["equals"])
            return (actual == want, f"头 {name} 期望 {want!r}，实际 {actual!r}")
        if "contains" in exp:
            want = str(exp["contains"])
            return (bool(actual) and want in actual,
                    f"头 {name} 期望含 {want!r}，实际 {actual!r}")
        return (False, f"头断言 {name} 需要 equals/contains")
    if "jsonPath" in exp:
        try:
            found, value = _json_path(json.loads(body_text) if body_text else None,
                                      str(exp["jsonPath"]))
        except ValueError as exc:
            return False, str(exc)
        except json.JSONDecodeError as exc:
            return False, f"响应体不是合法 JSON（{exc}），无法取 {exp['jsonPath']}"
        if exp.get("exists") is True:
            return (found, f"路径 {exp['jsonPath']} {'存在' if found else '不存在'}")
        rendered = repr(value)
        if "equals" in exp:
            want = exp["equals"]
            ok = found and value == want or (found and isinstance(value, (int, float))
                                             and isinstance(want, (int, float))
                                             and value == want)
            return (bool(ok), f"路径 {exp['jsonPath']} 期望 {want!r}，"
                              f"实际 {'<不存在>' if not found else rendered}")
        if "contains" in exp:
            want = str(exp["contains"])
            ok = found and isinstance(value, str) and want in value
            return (bool(ok), f"路径 {exp['jsonPath']} 期望含 {want!r}，"
                              f"实际 {'<不存在>' if not found else rendered}")
        return False, f"jsonPath 断言需要 exists/equals/contains 之一：{exp}"
    return False, f"不支持的 expect 写法：{exp}"


def run_api(params: dict, variables: dict[str, str]) -> dict:
    """执行一个 api 步骤，**永不抛异常**；返回 evidence dict（进 StepResult.api / run.json）。

    evidence 结构：method/url/status/duration_ms/passed/error/
    request{headers(打码)} / expect[{desc,passed}] / body_snippet / saved{...}
    """
    from .offline import require_online

    started = time.monotonic()
    evidence: dict = {"method": "GET", "url": "", "status": None,
                      "duration_ms": 0, "passed": False, "error": None,
                      "request": {}, "expect": [], "body_snippet": "", "saved": {}}
    try:
        require_online("api 步骤（HTTP 业务断言）")
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["duration_ms"] = int((time.monotonic() - started) * 1000)
        return evidence

    p = substitute_deep(params or {}, variables)
    method = str(p.get("method", "GET")).upper()
    url = str(p.get("url", "")).strip()
    evidence["method"] = method
    evidence["url"] = _redact_url(url)  # query 里凭据形态的键打码（P1-5③）

    try:
        if not url:
            raise ValueError("api 步骤缺少 url")
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"api 仅支持 http/https，收到 {scheme or '空'}://{url!r}")
        host = (urlparse(url).hostname or "").lower()
        if os.environ.get("MAT_API_DENY_PRIVATE_NETWORK", "").strip() == "1" \
                and (_PRIVATE_HOST.match(host) or host.endswith(".local")):
            raise ValueError(f"MAT_API_DENY_PRIVATE_NETWORK=1：拒绝内网地址 {host}")

        timeout_ms = int(p.get("timeoutMs") or _DEFAULT_TIMEOUT_MS)
        headers = {str(k): str(v) for k, v in (p.get("headers") or {}).items()}
        evidence["request"] = {"headers": {k: _mask(v, k) for k, v in headers.items()}}
        body = p.get("json")
        if body is not None:
            # 凭据形态的键打码（评审 P1-1）：登录类请求体最常见的就是密码，
            # 原样进证据等于把客户系统的凭据随报告外发
            evidence["request"]["json"] = _redact_json(body)

        resp = requests.request(
            method, url,
            headers=headers,
            json=body,
            timeout=max(0.1, timeout_ms / 1000),
        )
    except Exception as exc:  # noqa: BLE001 —— 网络/配置错误统一折进证据，不外抛
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["duration_ms"] = int((time.monotonic() - started) * 1000)
        return evidence

    evidence["status"] = resp.status_code
    evidence["duration_ms"] = int((time.monotonic() - started) * 1000)
    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    try:
        body_text = resp.text or ""
    except Exception:  # noqa: BLE001 —— 二进制响应等
        body_text = ""
    evidence["body_snippet"] = _redact_text(body_text)[:_MAX_BODY_SNIPPET]

    save_failures: list[str] = []
    saved: dict[str, str] = {}          # 运行时真值（写回 variables 供 ${var} 替换）
    saved_masked: dict[str, str] = {}   # 证据展示值（凭据形态打码）
    for var_name, jpath in (p.get("save") or {}).items():
        try:
            found, value = _json_path(json.loads(body_text) if body_text else None,
                                      str(jpath))
        except (ValueError, json.JSONDecodeError) as exc:
            save_failures.append(f"save {var_name}：{exc}")
            continue
        if not found:
            save_failures.append(f"save {var_name}：路径 {jpath} 不存在")
            continue
        saved[str(var_name)] = str(value)
        # 打码判据看**变量名或来源路径**（评审 P1-1：$.access_token 提出来的
        # 就算存进叫 leaked 的变量也是凭据）；运行时仍写真值供后续 ${var} 替换
        sensitive = (_SENSITIVE_KEY.search(str(var_name))
                     or _SENSITIVE_KEY.search(str(jpath)))
        saved_masked[str(var_name)] = "***" if sensitive else str(value)
    evidence["saved"] = saved_masked
    if saved:
        # 原地写回调用方传入的运行时变量表——后续步骤/断言用 ${var} 引用；
        # 写回是 run_api 的职责（runner 不重复做，避免两条写径漂移）。
        variables.update(saved)

    expects = p.get("expect") or []
    if not expects:
        # 没有 expect：2xx 即通过（纯取数/埋点型步骤的宽松默认）——
        # 但 **save 失败必须让步骤失败**（fail-closed）：变量取不到，
        # 后续步骤的 ${var} 替换一定错，宽松只会把错误推迟到更难查的地方。
        evidence["expect"] = [{"desc": d, "passed": False} for d in save_failures]
        ok_status = 200 <= (resp.status_code or 0) < 300
        evidence["passed"] = ok_status and not save_failures
        if not evidence["passed"]:
            parts = [] if ok_status else [f"未声明 expect，默认要求 2xx，实际 {resp.status_code}"]
            parts.extend(save_failures)
            evidence["error"] = "；".join(parts)
        return evidence

    all_ok = not save_failures
    evidence["expect"].extend({"desc": d, "passed": False} for d in save_failures)
    for exp in expects:
        if not isinstance(exp, dict):
            evidence["expect"].append({"desc": f"expect 必须是对象：{exp!r}",
                                       "passed": False})
            all_ok = False
            continue
        ok, desc = _check_expect(exp, resp.status_code, resp_headers, body_text)
        evidence["expect"].append({"desc": desc, "passed": bool(ok),
                                   **{k: v for k, v in exp.items()
                                      if k in ("status", "jsonPath", "header")}})
        all_ok = all_ok and ok
    evidence["passed"] = all_ok
    if not all_ok:
        evidence["error"] = "；".join(
            e["desc"] for e in evidence["expect"] if not e["passed"])
    return evidence
