#!/usr/bin/env python3
"""grounding 命中率评测 —— CLI 薄壳。

**口径不在这里**：实现已移入 `engine/insta_engine/grounding.py`（2026-11-24 转正，
评审 #12「没有复测命令」）。同一份实现既被 `mat grounding-eval` 使用，也被测试直接 import。

    python scripts/grounding_eval.py --data <shots.json> [--repeat 3] [--model X]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from insta_engine.grounding import main

if __name__ == "__main__":
    raise SystemExit(main())
