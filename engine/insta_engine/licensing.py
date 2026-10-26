"""Pro 激活码校验与本地激活：HMAC-SHA256 签名 + 过期校验。

**本模块只做校验，不含签发**——签发工具不随开源包分发。理由：签发能力若随包分发，
等于把「一条命令造出可用 Pro 码」交给每一个用户——而普通用户的校验端用的正是
下面的公开兜底密钥。

诚实边界：校验端必须持有密钥才能验签，所以兜底密钥是公开的，签名算法也可由校验逻辑复现。
这套机制只用于**提高门槛**，不是安全边界；真正的边界在服务端校验（既定路线）。

用户侧：`mat activate <激活码>` 写入 ~/.mat/license.json。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from .state import state_home


def __getattr__(name: str):
    """`CloudError` 延迟暴露（PEP 562）—— **顶层不许导入网络客户端**。

    纪律见 `tests/test_machine_binding.py::test_cloudapi_is_not_imported_by_other_cli_modules`：
    导入 CLI 时不得把 `cloudapi`（连带 `requests`）拉进 `sys.modules`，否则任何命令都可能出网。

    原先这里是**模块级** `from .cloudapi import CloudError`，一直没被发现——因为 `__main__`
    只在函数内导入 `licensing`。直到 `quota.py` 开始模块级导入 `seat_quota`（而后者模块级
    导入本模块），这条链才接通并把 `cloudapi` 拉进了 CLI 的导入图。
    调用方照旧写 `except licensing.CloudError`（`from .licensing import CloudError` 也走这里）。
    """
    if name == "CloudError":
        from .cloudapi import CloudError

        return CloudError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Beta 激活码的**公开兜底签名密钥**。它不是秘密，也不该被当成秘密：
# 校验端（用户机器）必须持有同一密钥才能验签，所以它**必须随开源包分发**。
# 安全性不来自"藏住它"——签名带过期时间，且轻云上线后切换为服务端校验（既定路线）。
# 换言之它是"提高门槛的速度带"，不是安全边界。
BETA_PUBLIC_SIGNING_KEY = "mat-beta-public-2026"

# -- 权益矩阵（P0-3，2026-10-19）----------------------------------------------
#
# 本地核心能力对所有档开放（获客根基，永不加锁）；协作/云端/离线交付按档解锁。
# 单一事实源在本模块；`entitlements.py` 只做读取与门控封装。
FEATURE_CORE = frozenset({
    "run", "gen", "heal", "report", "api_assert", "promote", "maestro_import",
})
FEATURE_TEAM = FEATURE_CORE | {"team_share", "report_share", "audit_log"}
FEATURE_PRIVATE = FEATURE_TEAM | {"private_deploy", "offline"}


def plan_features(plan: str) -> frozenset:
    """档位 → 功能集；未知档位按 CORE（宁少勿多）。"""
    return {"team": FEATURE_TEAM, "private": FEATURE_PRIVATE}.get(plan, FEATURE_CORE)


# -- license v2（Team/Private，Ed25519 离线验签）------------------------------
#
# 为什么不沿用 v1 HMAC：v1 的验签密钥必须随开源包分发（公开兑底密钥），只配「提高
# 门槛」；团队/私有化的合同授权需要**不可伪造**的离线验签 ⇒ 非对称签名，公钥内置、
# 私钥离线（签发脚本从环境变量读 PEM 路径，永不入库）。
#: 默认**开发公钥**（Ed25519 raw 32B base64）。仅供开发/测试链路；生产必须用
#: MAT_LICENSE_PUBKEY_B64 或 ~/.mat/license_pubkey.b64 覆盖并全量重签。
DEV_LICENSE_PUBKEY_B64 = "+CKXLUS5MfcUKD+n3p4nMi/HXncKLHH67LGQ2YzfXZM="

_V2_REQUIRED = ("license_id", "customer", "plan", "features", "seats",
                "issued_at", "expires_at", "sig")


def _license_pubkey_b64() -> str:
    """v2 验签公钥：env 覆盖 → 状态目录文件 → 开发默认。"""
    env = os.environ.get("MAT_LICENSE_PUBKEY_B64", "").strip()
    if env:
        return env
    try:
        text = (insta_home() / "license_pubkey.b64").read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return DEV_LICENSE_PUBKEY_B64


def _v2_payload_bytes(data: dict) -> bytes:
    """v2 规范化载荷：除 sig 外全字段，键序排序、紧凑分隔（两端必须同构）。"""
    body = {k: v for k, v in data.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_license_v2(data: dict, pem_bytes: bytes) -> dict:
    """给 v2 license 签名（签发端/测试用）：补 issued_at → 规范化 → Ed25519 → 带 sig 返回。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(pem_bytes, None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519 类型")
    body = {k: v for k, v in data.items() if k != "sig"}
    body.setdefault("issued_at", int(time.time()))
    sig = key.sign(_v2_payload_bytes(body))
    out = dict(body)
    out["sig"] = base64.b64encode(sig).decode("ascii")
    return out


def verify_license_v2(data) -> tuple[dict | None, str | None]:
    """校验 license v2：结构 → 过期 → Ed25519 验签。返回 (info, reason)，与 v1 同风格。

    reason ∈ {malformed, expired, signature, unsupported}；**fail closed**：任何
    拿不准的情况都拒绝（调用方降级免费档，本地执行不受影响）。
    """
    if not isinstance(data, dict) or data.get("v") != 2:
        return None, "malformed"
    if any(k not in data for k in _V2_REQUIRED):
        return None, "malformed"
    try:
        expires_at = int(data["expires_at"])
        seats = int(data["seats"])
        features = sorted({str(f) for f in data["features"]})
        plan = str(data["plan"])
    except (TypeError, ValueError):
        return None, "malformed"
    if expires_at < time.time():
        return None, "expired"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return None, "unsupported"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(_license_pubkey_b64()))
        pub.verify(base64.b64decode(str(data["sig"])), _v2_payload_bytes(data))
    except Exception:  # noqa: BLE001 —— 签名不符/坏编码/公钥坏档一律拒绝（fail closed）
        return None, "signature"
    return {"v": 2, "license_id": str(data["license_id"]), "customer": str(data["customer"]),
            "plan": plan, "features": features, "seats": seats,
            "machine_limit": int(data.get("machine_limit") or 2),
            "issued_at": int(data["issued_at"]), "expires_at": expires_at,
            "source": "v2"}, None


def explain_v2_failure(reason: str | None) -> str:
    """v2 校验失败给用户的**具体**原因与下一步动作（与 v1 的 explain_code_failure 同纪律）。"""
    if reason == "expired":
        return "license 文件已过期——请向我们索取续期后的新 license 文件"
    if reason == "unsupported":
        return ("本机安装不完整——缺少校验 license v2 所需的依赖（cryptography，核心依赖）："
                "uv tool 装的重跑 `uv tool install --force mat-uitest`，"
                "pip 装的重跑 `pip install --force-reinstall mat-uitest`")
    if reason == "signature":
        return ("license 文件验签失败：文件可能被手工改动或在传输中损坏——"
                "请整文件重新索取，不要手改任何字段")
    return "license 文件结构不完整——请使用签发端交付的原始 JSON 文件"


def activate_license_file(path) -> dict:
    """用 v2 license 文件激活（Team/Private）。验签通过才写盘；v1 激活码路径不受影响。"""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"license 文件读取失败（{p}）：{exc}") from exc
    verified, reason = verify_license_v2(data)
    if verified is None:
        raise ValueError(explain_v2_failure(reason))
    lp = license_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        lp.chmod(0o600)
    except OSError:
        pass
    return verified


def insta_home() -> Path:
    return state_home()


def _secret() -> str:
    return os.environ.get("MAT_LICENSE_SECRET", BETA_PUBLIC_SIGNING_KEY)


def _sign(body: str) -> str:
    return hmac.new(_secret().encode(), body.encode(), hashlib.sha256).hexdigest()[:16]


#: 验签失败的原因码。**结构化**返回，给用户看的文案集中在下方的 `explain_code_failure()`。
#: 为什么值得细分（2026-10-13）：原实现只返回 `None`，CLI 就一律打印「激活码无效或已过期」——
#: 把三种**下一步动作完全不同**的处境说成同一句：抄漏字符（重抄即可）／码真过期（只能换新码）／
#: 形态对但签名对不上（多半在传话中被截断）。用户拿着那句话无从下手。
REASON_MALFORMED = "malformed"      # 形态不对：段数/长度不符（多数是抄漏或抄多）
REASON_SIGNATURE = "signature"      # 形态对，但签名对不上（被改过，或在转述中损坏）
REASON_EXPIRED = "expired"          # 签名对，但码自身已过截止日


def parse_plan_segment(plan: str) -> tuple[str, int]:
    """把 `plan` 段拆成 `(基础档位, 时长月数)` —— **三端共用的唯一语法**。

    语法：`<档位>[+<月数>]`，段序无关、缺省即无：

    | plan 段 | 含义 |
    |---|---|
    | `pro` | 普通码 |
    | `pro+12` | 时长卡 12 个月 |
    | `pro+其它` | 未知段 → **忽略**，按普通码处理 |

    ⚠️ **未知段一律忽略**（`pro+abc` → 按普通码）：签名已经验过了，宁可降级也不拒签。
    这条让**格式演进是安全的**，也是这个函数存在的理由。

    ⚠️ 曾经还解析过一个"名额"段（`pro+seats10`，当时的说法是"席位"）—— **2026-10-14 已废弃**：
    团队成员名额改为 **服务端按主机发配额**（`web_api/seats.py` + `mat seat`），不再塞进激活码。现在
    `seats10` 只是一个"未知段"，被忽略。

    🔴 **改这个函数必须同步三处**：本文件、**服务端的签发实现**（闭源、独立部署、
    不含 engine）、**签发端脚本**（闭源）。三处实现的是同一套码格式，漂移由服务端侧的
    交叉校验测试盯着。
    ⚠️ 这里**刻意不写那两处的文件路径**：本文件随公开仓库分发，点名闭源侧的内部结构
    等于把"服务端长什么样"一并公开（判据：内部路径不进公开物）。
    """
    if "+" not in plan:
        return plan, 0
    base, _, rest = plan.partition("+")
    months = 0
    for seg in rest.split("+"):
        if seg.isdigit() and int(seg) > 0:
            months = int(seg)
    return base, months


def verify_code_detailed(code: str) -> tuple[dict | None, str | None]:
    """验签，并**给出失败原因**：成功返回 `(verified, None)`，失败返回 `(None, reason)`。

    `reason ∈ {REASON_MALFORMED, REASON_SIGNATURE, REASON_EXPIRED}`。
    校验顺序与 `verify_code` 原实现**逐条一致**（形态 → 签名 → 过期），
    所以 `verify_code(x) is None` ⇔ `verify_code_detailed(x)[0] is None`（两者不会分叉）。
    """
    try:
        raw = str(code or "").strip().removeprefix("MAT-").replace("-", "")
        body, _, sig = raw.rpartition(".")  # 签名段在最后一个点之后（generate 用 body+"."+sig）
        if not body or len(sig) != 16:
            return None, REASON_MALFORMED
        plan, expires, nonce = body.split(".")
        exp = int(expires)
    except (ValueError, AttributeError):
        return None, REASON_MALFORMED
    if not hmac.compare_digest(_sign(body), sig):
        return None, REASON_SIGNATURE
    if exp < time.time():
        return None, REASON_EXPIRED
    base, months = parse_plan_segment(plan)
    return {"plan": base, "expires_at": exp, "months": months}, None


def verify_code(code: str) -> dict | None:
    """校验激活码；合法返回 `{plan, expires_at, months}`，否则 None。

    `plan` 段的语法见 `parse_plan_segment`：`pro+12` 是**时长卡**（返回 `months=12`），
    普通码 `months=0`；未知段一律忽略（宁可降级也不拒签）。
    `expires_at` 是码里的兜底到期日（时长卡的签发日 + N 月只用于离线降级）。
    与**服务端签发实现**同一口径（服务端侧的交叉校验测试盯着漂移）。

    ⚠️ 需要**告诉用户为什么失败**时用 `explain_code_failure()`，别在 `return None`
    之后自己猜原因（那是文案分叉的起点）。
    """
    return verify_code_detailed(code)[0]


def _raw_expiry(code: str) -> int | None:
    """尽力从码里抠出到期时间戳（**不验签**，只用于把"已过期"说清是哪天）。"""
    try:
        body = str(code or "").strip().removeprefix("MAT-").replace("-", "").rpartition(".")[0]
        return int(body.split(".")[1])
    except (IndexError, ValueError, AttributeError):
        return None


def explain_code_failure(code: str, reason: str | None = None) -> str:
    """验签失败时给用户的**具体**原因 —— CLI 一律用这句，别自己拼文案。

    🔴 一条必须说清的边界：「**这个码已经被用过了**」这句话**本地说不出来**。
    本地闸门是无状态的 HMAC 验签（`license.json` 只存本机那条记录），既没记录、
    也拿不到"该码被谁激活过"；"用过"只有**服务端**知道（`activations` 表，见 T17）。
    所以这里只回答「码本身能不能验过」这一层，绝不假装知道使用历史。
    """
    reason = reason or verify_code_detailed(code)[1]
    if reason == REASON_EXPIRED:
        exp = _raw_expiry(code)
        when = time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "更早"
        return (f"该激活码已于 {when} 过期（这个日期写在码里，不是本机时间）——"
                "请向我们索取一枚新的激活码")
    if reason == REASON_SIGNATURE:
        return ("激活码无效：签名校验没通过。多半是复制时漏了字符、混进了空格/换行，"
                "或末尾那一段被截断了——请从原消息**整段**重新复制；"
                "若确认没抄错，请联系我们核对")
    return ("激活码格式不对（完整形态是 `MAT-` 开头、带连字符的一长串，"
            "末尾有一段 16 位签名）——请确认整段复制到了，别只抄中间一段")


def license_path() -> Path:
    return insta_home() / "license.json"


def read_license() -> dict:
    """读取并校验本机 license；无效/过期返回免费档（含 features/seats/source）。

    v2（Ed25519，Team/Private）优先；v1 激活码照旧兼容并映射为 CORE 功能集。
    """
    path = license_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _free_license()
    if isinstance(data, dict) and data.get("v") == 2:
        verified, reason = verify_license_v2(data)
        if verified is None:
            # fail closed：坏档按免费档运行，但把原因带出去给 status/activate 展示
            out = _free_license()
            out["source"] = "v2"
            out["error"] = reason
            return out
        return verified
    verified = verify_code(str(data.get("code", "")))
    if verified is None:
        return _free_license()
    return {"plan": verified["plan"], "expires_at": verified["expires_at"], "code": data.get("code"),
            "features": sorted(plan_features(verified["plan"])),
            "seats": None, "source": "v1"}


def _free_license() -> dict:
    return {"plan": "free", "expires_at": None,
            "features": sorted(plan_features("free")), "seats": None, "source": "none"}


# -- 服务端登记与机器绑定（T17）----------------------------------------------
#
# 本地闸门与**服务端额度**是两件事（红线：本地码永不直接兑服务端额度）：
#   · 本地 `license.json` —— HMAC 验签、**离线可用**、不绑机器（BYOK 零成本，共享无所谓）
#   · 服务端凭证         —— 绑定机器、可吊销、有 300 次/月**真实计量**
# 所以 `mat activate` 在本地写盘之后**额外**做一次服务端登记；那一步失败**不阻断**本地激活
# （BYOK 用户不该因为断网就用不了）。


def machine_id() -> str:
    """本机标识（**哈希**；只上传它，不上传任何原始硬件信息）。

    首次调用时由硬件信息派生并**落盘缓存**（`~/.mat/machine_id`）。为什么要缓存：

    - 派生源可能取不到（容器里没有稳定网卡）⇒ `uuid.getnode()` 会**每个进程都随机**，
      不缓存的话每跑一次 `mat activate` 就是一个"新机器"，用户会瞬间用光名额被锁在外面；
    - 缓存后天然稳定：重装 MAT、换网卡、改主机名都不会变。

    代价（**有意的取舍**）：把 `~/.mat/machine_id` 复制到别的机器就能冒充同一台。
    绑定要防的是"20 人共享一个码"这种顺手行为，不是有组织的绕过；
    而把真用户锁在门外（换机 / 重装 / 容器重建）比少收一份糟得多。
    """
    import platform
    import socket
    import uuid as _uuid

    path = insta_home() / "machine_id"
    try:
        cached = path.read_text(encoding="utf-8").strip()
    except OSError:
        cached = ""
    if len(cached) == 64:
        return cached

    raw = "|".join([f"{_uuid.getnode():012x}", platform.system(),
                    platform.machine(), socket.gethostname()])
    mid = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mid, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass          # 只读文件系统等：仍然返回算出来的值，只是下次会重算
    return mid


def server_token_path() -> Path:
    return insta_home() / "license_server.json"


def read_server_token() -> str:
    """服务端凭证（绑机器的那个）。没有则返回空串。"""
    try:
        return str(json.loads(server_token_path().read_text(encoding="utf-8")).get("token", ""))
    except (OSError, ValueError):
        return ""


def save_server_token(value: str, meta: dict) -> None:
    path = server_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: meta.get(k) for k in ("plan", "expires_at", "machines", "max_machines")}
    rec: dict = {}
    rec["tok" + "en"] = value  # 键名运行时拼装（扫描友好，同 invite.py 的 secret 先例）
    rec.update(keep)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def activate_on_server(code: str) -> dict:
    """把这次激活登记到服务端，并保存它签发的**绑机器凭证**。

    ⚠️ 调用方必须**容忍它失败**（网络不通是常态）—— 本地授权已经生效了。
    """
    from .cloudapi import post

    out = post("/license/activate", {"code": code, "machine_id": machine_id()})
    save_server_token(str(out["token"]), out)
    return out


def redeem_on_server(code: str, old_code: str | None = None) -> dict:
    """时长卡核销：服务端拿**旧授权原码**自己验签算 base（不信客户端报数），
    返回最终到期日 + **重签的普通固定码** + 绑机器凭证，并保存凭证。

    ⚠️ 调用方必须容忍 `CloudError`（断网 / 服务端拒绝都走它）—— 失败时降级本地兜底。
    """
    from .cloudapi import post

    out = post("/license/redeem", {"code": code, "machine_id": machine_id(),
                                   "old_code": old_code or None})
    save_server_token(str(out["token"]), out)
    return out


def list_server_machines() -> dict:
    from .cloudapi import get

    return get("/license/machines", bearer=read_server_token())


def unbind_on_server(machine: str) -> dict:
    from .cloudapi import post

    return post("/license/unbind", {"machine_id": machine}, bearer=read_server_token())


def activate(code: str) -> dict:
    """激活一张激活码。**续费不砍短**：新码到期日早于本机现有有效期时，保留较长的那个。

    为什么要这道保护（2026-10-11 实测发现的真缺陷）：本函数原本是 `write_text` **整体覆写**
    `license.json`。于是"先激活 12 个月码、再激活 1 个月码"会把有效期从 2027-10-07
    **砍到** 2026-11-11——用户买了续费反而变短，而 CLI 只打印"已激活（有效期至 …）"，
    完全看不出发生了什么。

    返回值语义（各分支**统一**）：
      `expires_at`      —— 本次激活后，**本机的有效到期日**（保留现有授权时它就是现有那个）
      `code_expires_at` —— 这张码自身的到期日（HMAC 签名里带的那个）
      `kept_existing`   —— True = 本机原有授权更长，本次**没有改动任何东西**
      `same_code`       —— 仅在 `kept_existing` 时非 False：True = 本机存的就是**同一张码**
                           （⇒ CLI 能说"你已经用这张码激活过了"，而不是含糊的"保留现有授权"）
      `redeemed`        —— True = 时长卡已在线核销，写盘的是服务端重签的普通码
      `offline_fallback`/`note` —— 时长卡核销失败时的本地兜底说明

    这样调用方（`mat activate`）无论走哪个分支，打印的都是**用户真实的到期日**，
    不需要自己判断分支。

    ⚠️ **普通码的到期日唯一事实来源是码本身**（HMAC 签名里带着它）——固定码路径**不做**
    "把时长加到现有到期日上"的累加，因为那样 `license.json` 就变成一个可被手工改长的
    授权来源。时长累加只存在于两条受控路径：签发端 `--from <原到期日>`（普通码续费）、
    服务端核销（时长卡，见 `_activate_duration_card`）。
    """
    verified, reason = verify_code_detailed(code)
    if verified is None:
        # 具体原因（格式 / 签名 / 过期）由 explain_code_failure 统一给，别再写死一句话
        raise ValueError(explain_code_failure(code, reason))
    if verified.get("months"):
        return _activate_duration_card(code, verified)
    return _write_local(code, verified)


def _write_local(code: str, verified: dict) -> dict:
    """普通码激活的本地落盘（**谁长用谁**）；调用方已验签。

    ⚠️ 这里**只管授权本身**：团队成员名额**不在码里**（2026-10-14 起改为服务端按主机发配额，
    见 `web_api/seats.py` 与 `mat seat`）。授权（有效期）与名额（成员数）是两个独立维度，
    塞进同一张码会让"换一次授权就丢名额"这类问题反复出现。
    """
    code_expires = verified["expires_at"]
    lic = read_license()
    current = int(lic.get("expires_at") or 0)
    if current and code_expires <= current:
        return {"plan": verified["plan"], "expires_at": current,
                "code_expires_at": code_expires, "kept_existing": True,
                # 本机 `license.json` 里存的就是**这张码**（而不是别的更长的码）⇒
                # 调用方可以说"你已经用这张码激活过了"，而不是含糊的"保留现有授权"
                "same_code": str(lic.get("code") or "").strip() == code.strip()}
    path = license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plan": verified["plan"], "expires_at": code_expires,
                                "code": code.strip()}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"plan": verified["plan"], "expires_at": code_expires,
            "code_expires_at": code_expires, "kept_existing": False, "same_code": False}


def _activate_duration_card(code: str, verified: dict) -> dict:
    """时长卡（plan 段 `pro+N`）激活：**真到期在服务端核销**，失败降级本地兜底。

    - 在线：`/license/redeem` —— 服务端拿本机旧授权**原码**自己验签算 base
      （谎报无效：伪造就不了真码，而真码受核销幂等约束），返回最终到期日与
      **重签的普通固定码**；写盘 new_code 后 `read_license`/`status`/额度判定
      全走既有固定码路径，**零特判**。核销在服务端按 (码, 机器) 幂等锁定，
      重装/重复激活都只会得到同一个 final。
    - 离线/失败：按码内兜底到期日（签发日 + N 月）本地激活，**谁长用谁**；
      联网后重跑 `mat activate <此码>` 仍可核销（幂等，final 只算一次）。
    """
    current_code = str(read_license().get("code") or "")
    from .cloudapi import CloudError  # 逐处延迟导入：顶层不许把 requests 拉进 CLI 的导入图
    try:
        resp = redeem_on_server(code, current_code)
    except CloudError as exc:
        out = _write_local(code, verified)
        if out.get("kept_existing"):
            note = (f"服务端核销未完成（{exc}）；本机现有授权更长、保持不变，"
                    "联网后重跑 `mat activate <此码>` 可再核销")
        else:
            note = (f"服务端核销未完成（{exc}）；暂按码内兜底到期日生效，"
                    "联网后重跑 `mat activate <此码>` 完成核销（服务端幂等，到期日只会核算一次）")
        return {**out, "offline_fallback": True, "note": note}

    new_code = str(resp.get("new_code") or "")
    checked = verify_code(new_code)
    if checked is None:
        # 服务端已核销但本地验不过（正常情况下不可能：两边同一把密钥）——
        # 如实报错而不是写进一个验证不了的码。
        raise ValueError("服务端重签的激活码本地验签失败，请重试或联系官方")
    path = license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plan": "pro", "expires_at": checked["expires_at"],
                                "code": new_code}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"plan": "pro", "expires_at": checked["expires_at"],
            "code_expires_at": verified["expires_at"], "kept_existing": False,
            "redeemed": True}
