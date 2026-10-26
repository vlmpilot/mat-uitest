"""用户状态主目录（license/usage/credentials/history 的共同根）。

历史名 `~/.insta` 已弃用为 `~/.mat`——首次访问自动整树迁移，旧目录保留作备份。
环境变量优先级：MAT_HOME > INSTA_HOME（旧变量，兼容测试与老用户）> 默认。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

#: 默认工作区的示例用例（mat init 与 ensure_default_workspace 共用的单源）。
EXAMPLE_CASE: dict = {
    "caseId": "case_example",
    "name": "设置冒烟",
    "target": {"package": "com.android.settings"},
    "steps": [
        {"id": "s1", "intent": "启动设置", "action": "launch", "params": {"package": "com.android.settings"}},
        {"id": "s2", "intent": "回到桌面", "action": "home"},
        {"id": "s3", "intent": "再次启动设置", "action": "launch", "params": {"package": "com.android.settings"}},
    ],
}


def ensure_default_workspace(root: Path) -> None:
    """把工作区铺到**开箱即用**：cases/ + artifacts/ + 示例用例 + prompt 模板。

    只对**我们自己的兜底工作区**（~/.mat/workspace）调用——用户显式指定的目录
    绝不代铺（示例文件在人家项目目录里是垃圾，不是引导）。幂等：示例用例已在
    即整体短路；已存在的文件一律不覆盖（与 mat init 同语义）。
    """
    example = root / "cases" / "example-settings.json"
    if example.exists():
        return
    (root / "cases").mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    example.write_text(json.dumps(EXAMPLE_CASE, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        from . import promptstore

        promptstore.seed(root / "prompts")
    except OSError:
        pass  # 播种失败不该挡住工作区解析——prompt 解析本就有包内 example 回落


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
