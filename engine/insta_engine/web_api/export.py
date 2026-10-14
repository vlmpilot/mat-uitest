"""数据导出域：/api/export（PIPL 数据可携带权，zip 打包，job 化）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .runtime import operator_of, start_job


def api_export(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/export：与 CLI `mat export` 同语义（--media 含截图由 body.media 决定）。"""
    media = bool(body.get("media"))

    def job(emit, cancel):
        import zipfile

        emit("export", "running", "打包本地数据（用例/图谱/报告/导入记录）")
        (root / "artifacts").mkdir(parents=True, exist_ok=True)
        out = root / "artifacts" / \
            f"mat-export-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.zip"
        count = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            patterns = ["cases/*.json", "artifacts/page_index/*.json",
                        "artifacts/imports/*/import.json"]
            for pattern in patterns:
                for f in root.glob(pattern):
                    zf.write(f, f.as_posix())
                    count += 1
            for run_json in (root / "artifacts").glob("*/run.json"):
                zf.write(run_json, run_json.as_posix())
                count += 1
            if media:
                for shot in (root / "artifacts").glob("*/screenshots/*.png"):
                    zf.write(shot, shot.as_posix())
                    count += 1
        return f"已导出 {count} 个文件", f"/artifacts/{out.name}"

    return 200, {"job_id": start_job("export", job, meta={"operator": operator_of(body)})}
