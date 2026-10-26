"""P0-5：大截图走缩略图内联 + 原图链接；30 步报告 ≤3MB；小图仍内联原图。"""

from __future__ import annotations

import io
import os

from PIL import Image

from insta_engine.report import render_html
from insta_engine.runner import RunResult, StepResult


def _noisy_png_bytes(width=600, height=800) -> bytes:
    """随机噪声 PNG（纯色图压完只有几 KB，测不出预算路径）。"""
    raw = os.urandom(width * height * 3)
    im = Image.frombytes("RGB", (width, height), raw)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_big_screenshots_use_thumbnails_and_stay_small(tmp_path):
    big = _noisy_png_bytes()
    assert len(big) > 200_000, "测试前置：噪声 PNG 应大于原图内联上限"

    steps = []
    for i in range(30):
        rel = f"screenshots/s{i}.png"
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(big)
        steps.append(StepResult(step_id=f"s{i}", intent=f"步骤{i}", action="tap",
                                status="passed", screenshot=rel))
    run = RunResult(case_id="c1", case_name="大图回归", status="passed",
                    step_success_rate=1.0, duration_ms=30_000, steps=steps)

    out = render_html(run, tmp_path)
    size = out.stat().st_size
    assert size < 3 * 1024 * 1024, f"报告 {size} 字节，超出 3MB 交付预算"
    html = out.read_text(encoding="utf-8")
    assert "data:image/jpeg" in html, "应有缩略图内联"
    assert "原图" in html, "应有原图链接（缩略图之外的证据通道）"
    assert (tmp_path / "summary.md").is_file()


def test_small_screenshot_inlines_original(tmp_path):
    small = _noisy_png_bytes(60, 80)
    rel = "screenshots/s.png"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(small)
    run = RunResult(case_id="c", status="passed", step_success_rate=1.0,
                    steps=[StepResult(step_id="s1", action="tap", status="passed",
                                      screenshot=rel)])
    html = render_html(run, tmp_path).read_text(encoding="utf-8")
    assert "data:image/png" in html
    assert "原图" not in html
