"""运行结果通知（验收项的实现）：飞书/钉钉/自定义 webhook。

配置（.env 或环境变量）：MAT_WEBHOOK_URL + MAT_NOTIFY_FORMAT（feishu|dingtalk|generic）。
best-effort：失败不影响执行主流程。
"""

from __future__ import annotations

import os

import requests


def configured() -> bool:
    return bool(os.environ.get("MAT_WEBHOOK_URL"))


def _payload(fmt: str, text: str) -> dict:
    if fmt == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if fmt == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    return {"event": "mat.run.finished", "text": text}


def send_run_finished(summary: str, webhook_url: str | None = None,
                      fmt: str | None = None) -> bool:
    url = webhook_url or os.environ.get("MAT_WEBHOOK_URL")
    if not url:
        return False
    fmt = (fmt or os.environ.get("MAT_NOTIFY_FORMAT", "generic")).lower()
    try:
        resp = requests.post(url, json=_payload(fmt, summary), timeout=5)
        return resp.status_code < 400
    except Exception:
        return False
