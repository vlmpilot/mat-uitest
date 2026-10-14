"""凭据安全存储 v0。

~/.mat/credentials.json（0600）；用例中 ${cred.名称} 注入，报告与日志不回显值。
KMS/字段级加密在轻云/企业版落地。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .quota import insta_home


class CredentialStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (insta_home() / "credentials.json")

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def load_as_vars(self) -> dict[str, str]:
        """以 ${cred.名称} 可引用的键形态返回。"""
        return {"cred." + name: value for name, value in self._read().items()}

    def names(self) -> list[str]:
        return sorted(self._read())

    def set(self, name: str, value: str) -> None:
        data = self._read()
        data[name] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # 非 POSIX 文件系统（如部分 Windows 配置）

    def delete(self, name: str) -> bool:
        data = self._read()
        if name not in data:
            return False
        del data[name]
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
