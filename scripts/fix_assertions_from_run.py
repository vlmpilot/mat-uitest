#!/usr/bin/env python3
"""从真机失败现场给断言提候选 —— CLI 薄壳。

**判据不在这个文件里**：实现已移入 `engine/insta_engine/assertion_fix.py`
（2026-11-24 转正，评审 #5）。同一份实现既被 `mat fix-assertions` 使用，
也被测试直接 import——不要在这里抄第二份。

    python scripts/fix_assertions_from_run.py <run.json...> [--out DIR] [--json FILE]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from insta_engine.assertion_fix import (  # noqa: F401
    apply_fix,
    candidates,
    main,
    propose,
)

if __name__ == "__main__":
    raise SystemExit(main())
