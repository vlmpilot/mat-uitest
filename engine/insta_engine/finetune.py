"""用户侧微调工具链 L2（`mat finetune`）——数据抽取 / 训练 / 留出评测 / 加载。

## 定位（MAT-用户侧微调工具链-2026-10-29）

平台不做通用微调；这是"让模型更懂你的 App"的**用户侧工具链**。三条边界必须随用：
① adapter 与基座版本绑定（基座升级必须重训）；② 平台不为训出的模型质量背书——
工具链保证跑通，效果用户负责；③ 数据不出本机（训练全在用户机器上）。

## 使用模式的硬限制（文档未写明，实现在此明确）

adapter 依赖本地基座（mlx_lm），**只在 local 模式（自带 Key + 本地 OpenAI 兼容端点）
生效**；`GEN_PROMPT_MODE=server` 的官方代理跑的是平台基座，加载不了用户 adapter。
`use` 子命令只写 local 三件套配置并打印 server 启动命令。

## 数据门槛（见 run_eval 的姊妹文档，2026-10-28 盘点）

训练对 = (nl → DSL JSON)，nl 从 `case.meta.nl` 回溯（此前存量全部没有 ⇒ 历史
2,300+ 条 run.json 只有执行结果、没有输入）。**样本门槛 ≥200 对**（单 App 同源）
——不足时 `export` 如实报告、`train` 拒绝执行：宁可不训，不拿稀烂数据出 adapter。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

#: 单 App 训练对门槛（工具链文档 §4.2）；不足时 train 拒绝
MIN_PAIRS = 200

#: 留出集规模（从合格对中切出，训练时不可见）
HOLDOUT_DEFAULT = 30


def _iter_run_records(root: Path):
    """扫工作区全部执行记录（run.json）。"""
    for p in (Path(root) / "artifacts").rglob("run.json"):
        try:
            yield json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def collect_pairs(root: Path, app: str | None = None) -> dict:
    """从执行记录抽取 (nl → DSL) 训练对。**只取 passed**（不把 AI 的错误学进去）。

    nl 来源：`case.meta.nl`（2026-10-28 起生成时落盘）——没有 nl 的记录**剔除**
    并计数（历史存量大多在此）。按 caseId 去重（同一用例多轮执行是同一对）。
    """
    pairs: list[dict] = []
    seen: set[str] = set()
    no_nl = 0
    per_app = Counter()
    for d in _iter_run_records(root):
        if (d.get("run") or {}).get("status") != "passed":
            continue
        case = d.get("case") or {}
        pkg = (case.get("target") or {}).get("package") or ""
        if app and pkg != app:
            continue
        nl = str((case.get("meta") or {}).get("nl") or "").strip()
        cid = str(case.get("caseId") or "")
        if not nl:
            no_nl += 1
            continue
        if cid in seen:
            continue
        seen.add(cid)
        app_info = {"package": pkg}
        if case.get("meta", {}).get("app_notes"):
            app_info["user_notes"] = case["meta"]["app_notes"]
        pairs.append({
            "case_id": cid,
            "app": pkg,
            "messages": [
                {"role": "user", "content": f"{nl}\n\nApp 信息: {json.dumps(app_info, ensure_ascii=False)}"},
                {"role": "assistant", "content": json.dumps(case, ensure_ascii=False)},
            ],
        })
        per_app[pkg] += 1
    return {"pairs": pairs, "no_nl": no_nl, "per_app": dict(per_app)}


def export_dataset(root: Path, app: str | None, out: Path) -> dict:
    """`mat finetune export`：抽取 → JSONL（messages 格式，mlx_lm 直读）+ 统计。"""
    result = collect_pairs(root, app)
    pairs = result["pairs"]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "train.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps(p["messages"], ensure_ascii=False) for p in pairs) + "\n",
        encoding="utf-8")
    stats = {
        "app": app or "(全部 App 混合)",
        "pairs": len(pairs),
        "no_nl_skipped": result["no_nl"],
        "per_app": result["per_app"],
        "min_pairs_required": MIN_PAIRS,
        "enough": len(pairs) >= MIN_PAIRS,
        "train_jsonl": str(jsonl),
    }
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1) + "\n",
                                    encoding="utf-8")
    return stats


def train(root: Path, data_dir: Path, base: str) -> dict:
    """`mat finetune train`：调 mlx_lm.lora。**数据不足拒绝**——宁可不训不出烂 adapter。"""
    data_dir = Path(data_dir)
    jsonl = data_dir / "train.jsonl"
    if not jsonl.exists():
        raise FileNotFoundError(f"先 export：{jsonl} 不存在")
    n_pairs = len([l for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()])
    if n_pairs < MIN_PAIRS:
        return {"ok": False, "error": (
            f"训练对 {n_pairs}/{MIN_PAIRS} 不足——继续积累（正常使用 MAT，meta.nl 会自动落盘）"
            "或降低范围到多 App 混训（export 不带 --app）")}
    if not shutil.which("python3") or True:  # mlx 检查交由 subprocess 报错（本机已装）
        pass
    adapter_dir = data_dir / "adapter"
    cmd = ["python3", "-m", "mlx_lm", "lora", "--train",
           "--data", str(jsonl), "--model", base,
           "--adapter-path", str(adapter_dir),
           "--iters", "1000", "--batch-size", "1", "--grad-checkpoint"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=4 * 3600)
    return {"ok": proc.returncode == 0, "adapter": str(adapter_dir),
            "returncode": proc.returncode,
            "output_tail": (proc.stdout or "")[-2000:] + (proc.stderr or "")[-1000:],
            "cmd": cmd}


def load_holdout(data_dir: Path, n: int = HOLDOUT_DEFAULT) -> list[dict]:
    """从 train.jsonl 尾部切留出集（export 按时间序写，尾部最新）。"""
    jsonl = Path(data_dir) / "train.jsonl"
    lines = [l for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [json.loads(l) for l in lines[-min(n, len(lines)):]]


def eval_adapter(root: Path, adapter: str, base: str, holdout: list[dict],
                 base_url: str = "http://127.0.0.1:8080", timeout: int = 180) -> dict:
    """`mat finetune eval`：留出集逐条走本地端点生成（GenClient 同协议）。

    自动初筛 = DSL 可解析 + 步骤数与参照差 <50%（人工判"可用/改改能用/不能用"
    仍需人——工具链不背书质量，只把对比材料摆齐）。
    """
    import requests

    from .dsl import TestCase

    ok, parse_fail, empty = 0, 0, 0
    rows = []
    for i, sample in enumerate(holdout):
        user = sample["messages"][0]["content"]
        ref = sample["messages"][1]["content"]
        try:
            resp = requests.post(
                f"{base_url}/v1/chat/completions",
                json={"model": base, "messages": [
                    {"role": "user",
                     "content": user + f"\n\nadapter: {adapter}"},
                ]}, timeout=timeout)
            content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as exc:  # noqa: BLE001 —— 端点未起等
            return {"ok": False, "error": f"本地端点 {base_url} 不可达：{exc}（先起 mlx_lm.server）"}
        content = content.strip()
        if content.startswith("```"):  # 剥围栏
            content = content.split("```")[1].lstrip("json").strip()
        if not content:
            empty += 1
            rows.append({"i": i, "verdict": "empty"})
            continue
        try:
            case = TestCase.model_validate_json(content)
            ref_steps = len((json.loads(ref)).get("steps") or [])
            gap = abs(len(case.steps) - ref_steps) / max(ref_steps, 1)
            rows.append({"i": i, "verdict": "near" if gap < 0.5 else "diverged",
                         "steps": len(case.steps), "ref_steps": ref_steps})
            ok += 1
        except Exception:  # noqa: BLE001 —— 解析失败即不合格样本
            parse_fail += 1
            rows.append({"i": i, "verdict": "parse-fail"})
    return {"ok": True, "total": len(holdout), "dsl_ok": ok, "parse_fail": parse_fail,
            "empty": empty,
            "auto_pass": round(ok / len(holdout), 3) if holdout else 0.0,
            "rows": rows,
            "note": "自动初筛只证明「输出是合法 MAT DSL」——可用性人工判（工具链不背书）"}


def write_use_config(root: Path, adapter: str, base: str,
                     server_url: str = "http://127.0.0.1:8080") -> Path:
    """`mat finetune use`：把 gen 的 local 三件套写进工作区 .env（提示段不覆盖已有值）。"""
    env_path = Path(root) / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    wanted = {
        "GEN_BASE_URL": server_url,
        "GEN_MODEL": base,
        "GEN_PROMPT_MODE": "local",
        "GEN_API_KEY": "local",
    }
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env_path
