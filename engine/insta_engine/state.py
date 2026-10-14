"""用户状态主目录（license/usage/credentials/history 的共同根）。

历史名 `~/.insta` 已弃用为 `~/.mat`——首次访问自动整树迁移，旧目录保留作备份。
环境变量优先级：MAT_HOME > INSTA_HOME（旧变量，兼容测试与老用户）> 默认。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def state_home() -> Path:
    env_m = os.environ.get("MAT_HOME")
    env_i = os.environ.get("INSTA_HOME")
    if env_m:
        return Path(env_m)
    if env_i:
        return Path(env_i)
    new = Path.home() / ".mat"
    legacy = Path.home() / ".insta"
    if not new.exists() and legacy.is_dir():
        shutil.copytree(legacy, new, dirs_exist_ok=True)
    return new
