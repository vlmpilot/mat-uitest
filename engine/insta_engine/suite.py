"""用例/套件加载：`run` 命令的入口解析（单测覆盖）。

套件 JSON: {"name": "...", "cases": ["cases/a.json", {"caseId": ...内联...}]}
单用例 JSON: TestCase 本体（含 "steps"）。
"""

from __future__ import annotations

import json
from pathlib import Path

from .dsl import TestCase


def load_case_or_suite(path: Path) -> tuple[str, object]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "cases" in data:
        if not isinstance(data["cases"], list) or not data["cases"]:
            raise ValueError(f"套件 {path} 的 cases 必须为非空数组")
        return "suite", data
    return "case", TestCase.model_validate(data)
