#!/usr/bin/env python3
"""grounding 命中率评测：视觉定位返回的坐标是否落在真值框内。

定义见 `benchmarks/METRICS.md` 第 7 项。真值来自控件树 `bounds`（见
`scripts/grounding_capture.py` 的说明），不是人工标注。

## 用法

    # 需要模型凭据（与 mat run 同一套环境变量）
    export GEN_API_KEY=... GEN_BASE_URL=... GEN_MODEL=qwen3-vl-plus
    export GEN_VL_MODEL=qwen3-vl-plus GEN_PROMPT_MODE=local

    python scripts/grounding_eval.py \
        --data benchmarks/grounding/demo-app/shots.json \
        --out benchmarks/grounding/demo-app/result-$(date +%F).json

## 判定口径（写死在这里，避免每次解释）

- **命中**：返回坐标落在真值框内（`--tolerance-px` 可放宽，默认 0 = 严格）。
- **未命中**：坐标在框外，**或**模型返回 `found=false`（它认为目标不在图上）。
  后者单独计数，因为"看不见"和"看错位置"是两种能力问题。
- 分母 = 所有目标 × 重复次数；失败调用（API 报错）单独计，**不进分母**
  （与"skipped 不进分母"同一纪律）。

## 为什么不用缓存

同一张图 + 同一 target 命中缓存会让重复次数退化成 1，测不出抖动。
`cache_key=None` 显式关掉。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, center - half), min(1.0, center + half))


def hit(point: list[float] | None, box: list[int], tol: int) -> bool:
    if not point:
        return False
    x, y = point[0], point[1]
    x1, y1, x2, y2 = box
    return (x1 - tol) <= x <= (x2 + tol) and (y1 - tol) <= y <= (y2 + tol)


def summarize(rows: list[dict]) -> dict:
    valid = [r for r in rows if r.get("error") is None]
    hits = [r for r in valid if r["hit"]]
    not_found = [r for r in valid if r["found"] is False]

    def group(key: str) -> dict:
        buckets: dict[str, list[dict]] = {}
        for r in valid:
            buckets.setdefault(r[key], []).append(r)
        out = {}
        for name, rs in sorted(buckets.items()):
            k = sum(1 for r in rs if r["hit"])
            lo, hi = wilson(k, len(rs))
            out[name] = {"n": len(rs), "hits": k,
                         "rate": round(k / len(rs), 4) if rs else None,
                         "wilson": [round(lo, 4), round(hi, 4)]}
        return out

    lo, hi = wilson(len(hits), len(valid))
    return {
        "n": len(valid),
        "hits": len(hits),
        "rate": round(len(hits) / len(valid), 4) if valid else None,
        "wilson": [round(lo, 4), round(hi, 4)],
        "not_found": len(not_found),
        "errors": len(rows) - len(valid),
        "by_bucket": group("bucket"),
        "by_kind": group("kind"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grounding 命中率评测（真值来自控件树 bounds）")
    ap.add_argument("--data", required=True, help="shots.json（grounding_capture.py 的产物）")
    ap.add_argument("--out", help="结果 JSON 路径（缺省打印到 stdout）")
    ap.add_argument("--repeat", type=int, default=1, help="每个目标重复次数（测抖动用）")
    ap.add_argument("--tolerance-px", type=int, default=0, help="真值框容差（像素）")
    args = ap.parse_args(argv)

    data_path = pathlib.Path(args.data)
    data = json.loads(data_path.read_text(encoding="utf-8"))
    root = data_path.parent

    from insta_engine import vision  # 延迟导入：没装依赖时先给出清晰报错

    if not vision.enabled():
        print("视觉未启用：检查 GEN_API_KEY / GEN_VL_MODEL / GEN_BASE_URL", file=sys.stderr)
        return 2
    client = vision.default_client()

    rows: list[dict] = []
    for shot in data["shots"]:
        png = (root / shot["png"]).read_bytes()
        for target in shot["targets"]:
            for i in range(max(1, args.repeat)):
                t0 = time.time()
                error = None
                point = None
                found = None
                conf = None
                try:
                    point = vision.locate(client, png, target["intent"], target["desc"],
                                          cache_key=None, tree_evidence=True)
                    lr = vision.last_result() or {}
                    found = lr.get("found")
                    conf = lr.get("confidence")
                except Exception as exc:  # noqa: BLE001 —— 调用失败单独计，不进分母
                    error = f"{type(exc).__name__}: {exc}"
                rows.append({
                    "shot_id": shot["shot_id"],
                    "target_id": target["target_id"],
                    "bucket": target["bucket"],
                    "kind": target["kind"],
                    "bbox": target["bbox"],
                    "point": point,
                    "found": found if error is None else None,
                    "confidence": conf,
                    "hit": hit(point, target["bbox"], args.tolerance_px) if error is None else False,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": error,
                })
                mark = "✓" if rows[-1]["hit"] else ("?" if error else "✗")
                print(f"  {mark} {shot['shot_id']}/{target['target_id']:<18}"
                      f"pt={point} found={found} {rows[-1]['latency_ms']}ms")

    result = {
        "schema_version": 1,
        "data": str(data_path),
        "app": data.get("app"),
        "apk_sha256": data.get("apk_sha256"),
        "device": data.get("device"),
        "png_size": data.get("png_size"),
        "model": _model_name(),
        "repeat": args.repeat,
        "tolerance_px": args.tolerance_px,
        "overall": summarize(rows),
        "results": rows,
    }
    text = json.dumps(result, ensure_ascii=False, indent=1) + "\n"
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
        o = result["overall"]
        print(f"\n命中 {o['hits']}/{o['n']} = {o['rate']:.1%}"
              f"（95% Wilson {o['wilson'][0]:.1%}–{o['wilson'][1]:.1%}）"
              f" | found=false {o['not_found']} | 调用失败 {o['errors']}")
        print(f"→ {args.out}")
    else:
        print(text)
    return 0


def _model_name() -> str:
    import os
    return os.environ.get("GEN_VL_MODEL") or os.environ.get("GEN_MODEL") or ""


if __name__ == "__main__":
    sys.exit(main())
