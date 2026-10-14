"""凭据域：/api/cred（GET 列名单 / POST set|del）。用例中以 ${cred.名称} 引用。

⚠️ 凭据管理**仅限本机**（`_local_only`）——`--share` 局域网暴露时禁用（防凭据被局域网改写）。
值永不回传（列表只给名字）。
"""

from __future__ import annotations

from ..credentials import CredentialStore
from .workspace import _local_only


def api_cred_list(host: str) -> tuple[int, dict]:
    gate = _local_only(host)
    if gate:
        return gate
    return 200, {"names": sorted(CredentialStore().names())}


def api_cred_write(host: str, body: dict) -> tuple[int, dict]:
    gate = _local_only(host)
    if gate:
        return gate
    action = str(body.get("action", "")).strip()
    name = str(body.get("name", "")).strip()
    value = str(body.get("value", ""))
    if not name or "/" in name or ".." in name:
        return 400, {"error": "非法凭据名"}
    store = CredentialStore()
    if action == "set":
        if not value:
            return 400, {"error": "缺少 value"}
        store.set(name, value)
        return 200, {"ok": True, "names": sorted(store.names())}
    if action == "del":
        removed = store.delete(name)
        return 200, {"ok": removed, "names": sorted(store.names())}
    return 400, {"error": f"未知 action {action!r}（set/del）"}
