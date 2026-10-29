"""用户侧微调工具链 L2（`mat finetune`）——数据抽取 / 训练 / 留出评测 / 加载。

## 定位（MAT-用户侧微调工具链-2026-10-29）

平台不做通用微调；这是"让模型更懂你的 App"的**用户侧工具链**。三条边界必须随用：
① adapter 与基座版本绑定（基座升级必须重训）；② 平台不为训出的模型质量背书——
工具链保证跑通，效果用户负责；③ 数据不出本机（训练全在用户机器上）。

## 使用模式的硬限制（文档未写明，实现在此明确）

adapter 依赖本地基座（mlx_lm），**只在 local 模式（自带 Key + 本地 OpenAI 兼容端点）
生效**；`GEN_PROMPT_MODE=server` 的官方代理跑的是平台基座，加载不了用户 adapter。
`use` 子命令只写 local 三件套配置并打印 server 启动命令。

## 数据门槛（2026-10-28 盘点 + 2026-10-29 review 修正）

训练对 = (nl → DSL JSON)，nl 从 `case.meta.nl` 回溯（此前存量全部没有 ⇒ 历史
2,300+ 条 run.json 只有执行结果、没有输入）。**可训练对门槛 ≥200**（train + valid，
不含留出）——不足时 `export` 如实报告、`train` 拒绝执行：宁可不训，不拿稀烂数据出 adapter。

## 数据切分（review P0-2）

`export` 把合格对**一次性切成三份互斥文件**：`train.jsonl` / `valid.jsonl` /
`holdout.jsonl`。留出集必须训练不可见——此前 `eval` 从 `train.jsonl` 尾部现切，
与训练集 100% 重叠，通过率必然虚高。三份的 `messages` 是 mlx_lm 的 chat 格式
（每行 `{"messages": [...]}`，**不是裸数组**，review P0-1）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

#: 可训练对门槛（工具链文档 §4.2）；不足时 train 拒绝
MIN_PAIRS = 200

#: 留出集规模上限（从合格对尾部切出，训练时不可见）
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

    assistant 侧**剔除 meta**（review P0-4）：`meta.nl` 就是 user 侧原文，整包序列化
    等于把输入塞进标签，模型会学到「输出 DSL 时复述需求」，还会输出执行链路并不
    需要的 meta 字段。
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
        # 标签只留执行相关字段：meta 既要剔除 nl 泄漏，也不是 DSL 的必需部分
        label = {k: v for k, v in case.items() if k != "meta"}
        pairs.append({
            "messages": [
                {"role": "user",
                 "content": f"{nl}\n\nApp 信息: {json.dumps(app_info, ensure_ascii=False)}"},
                {"role": "assistant", "content": json.dumps(label, ensure_ascii=False)},
            ],
            # 行级元数据：不进 messages（不参与训练），仅供 eval 还原 nl / app_info
            "meta": {"case_id": cid, "app": pkg, "nl": nl, "app_info": app_info},
        })
        per_app[pkg] += 1
    return {"pairs": pairs, "no_nl": no_nl, "per_app": dict(per_app)}


def split_pairs(pairs: list[dict]) -> dict:
    """把合格对切成三份互斥集合：train / valid / holdout。

    规则：按 caseId 稳定排序 → 尾部 30 条（且不超过 1/5）作留出集 → 剩余再切
    10%（至少 1 条）作 valid → 其余为 train。留出集**绝不参与训练**。
    """
    ordered = sorted(pairs, key=lambda p: str((p.get("meta") or {}).get("case_id") or ""))
    n = len(ordered)
    if not n:
        return {"train": [], "valid": [], "holdout": []}
    holdout_n = min(HOLDOUT_DEFAULT, max(1, n // 5))
    holdout = ordered[n - holdout_n:]
    rest = ordered[: n - holdout_n]
    valid_n = max(1, len(rest) // 10) if len(rest) >= 10 else 0
    valid = rest[len(rest) - valid_n:] if valid_n else []
    train = rest[: len(rest) - valid_n]
    return {"train": train, "valid": valid, "holdout": holdout}


def _jsonl_line(pair: dict) -> str:
    """mlx_lm chat 数据集的一行：`{"messages": [...]}`（多余键被忽略）。"""
    return json.dumps({"messages": pair["messages"], "meta": pair.get("meta") or {}},
                      ensure_ascii=False) + "\n"


def export_dataset(root: Path, app: str | None, out: Path) -> dict:
    """`mat finetune export`：抽取 → train/valid/holdout 三份 JSONL + 统计。"""
    result = collect_pairs(root, app)
    pairs = result["pairs"]
    split = split_pairs(pairs)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in ("train", "valid", "holdout"):
        path = out / f"{name}.jsonl"
        path.write_text("".join(_jsonl_line(p) for p in split[name]), encoding="utf-8")
        paths[name] = str(path)
    trainable = len(split["train"]) + len(split["valid"])
    stats = {
        "app": app or "(全部 App 混合)",
        "pairs": len(pairs),
        "train_pairs": trainable,
        "holdout_pairs": len(split["holdout"]),
        "no_nl_skipped": result["no_nl"],
        "per_app": result["per_app"],
        "min_pairs_required": MIN_PAIRS,
        "enough": trainable >= MIN_PAIRS,
        "train_jsonl": paths["train"],
        "valid_jsonl": paths["valid"],
        "holdout_jsonl": paths["holdout"],
    }
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1) + "\n",
                                    encoding="utf-8")
    return stats


def _count_lines(path: Path) -> int:
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def train(root: Path, data_dir: Path, base: str, python: str | None = None) -> dict:
    """`mat finetune train`：调 mlx_lm.lora。**数据不足拒绝**——宁可不训不出烂 adapter。

    解释器默认取**当前进程**（`sys.executable`），而不是 PATH 里的 `python3`：
    用户通常把 mlx-lm 装进 venv，硬编码 `python3` 会落到另一个没有它的解释器
    （2026-10-29 本机实测：`python3` → miniconda，无 mlx_lm）。
    """
    data_dir = Path(data_dir)
    jsonl = data_dir / "train.jsonl"
    if not jsonl.exists():
        raise FileNotFoundError(f"先 export：{jsonl} 不存在")
    valid = data_dir / "valid.jsonl"
    n_pairs = _count_lines(jsonl) + (_count_lines(valid) if valid.exists() else 0)
    if n_pairs < MIN_PAIRS:
        return {"ok": False, "error": (
            f"可训练对 {n_pairs}/{MIN_PAIRS} 不足——继续积累（正常使用 MAT，meta.nl 会自动落盘）"
            "或降低范围到多 App 混训（export 不带 --app）")}
    py = python or sys.executable
    adapter_dir = data_dir / "adapter"
    # 有 valid.jsonl 走目录模式（mlx_lm 同时读 train/valid）；没有则退回单文件
    data_arg = str(data_dir) if valid.exists() else str(jsonl)
    cmd = [py, "-m", "mlx_lm", "lora", "--train",
           "--data", data_arg, "--model", base,
           "--adapter-path", str(adapter_dir),
           "--iters", "1000", "--batch-size", "1", "--grad-checkpoint"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=4 * 3600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "训练超过 4 小时被中止（可用 --iters 更小的配置手工调）",
                "cmd": cmd}
    except OSError as exc:
        return {"ok": False, "error": f"解释器不可用：{py}（{exc}）——用 --python 指定",
                "cmd": cmd}
    tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-1000:]
    if proc.returncode == 0:
        return {"ok": True, "adapter": str(adapter_dir), "returncode": 0,
                "output_tail": tail, "cmd": cmd}
    error = f"mlx_lm.lora 退出码 {proc.returncode}"
    if "No module named mlx_lm" in tail:
        error = f"{py} 没有 mlx_lm——先 `pip install mlx-lm`，或用 --python 指定装了它的解释器"
    return {"ok": False, "error": error, "adapter": str(adapter_dir),
            "returncode": proc.returncode, "output_tail": tail, "cmd": cmd}


def load_holdout(data_dir: Path, n: int = 0) -> list[dict]:
    """读 export 切出的 `holdout.jsonl`（训练不可见）；n>0 时只取前 n 条。"""
    path = Path(data_dir) / "holdout.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"留出集不存在：{path}——先运行 `mat finetune export`")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return rows[:n] if n > 0 else rows


def _run_holdout(root: Path, base: str, holdout: list[dict], base_url: str,
                 timeout: int) -> dict:
    """用**与 gen 完全相同的链路**跑一遍留出集（review P0-3）。

    此前 eval 是裸 HTTP + 手拼 prompt（`user + "adapter: <路径>"`），没有 system
    提示与输出约束，还把 adapter 目录当 prompt 内容塞进去——测的不是部署时会用的
    链路。这里直接复用 `GenClient.generate`：同一模板、同一校验、同一后处理。
    """
    from . import promptstore
    from .ai_client import GenClient, GenError

    prompt_path = promptstore.resolve("gen", Path(root))
    client = GenClient(base_url, "local", base, prompt_path=prompt_path, prompt_mode="local")
    rows: list[dict] = []
    ok = diverged = parse_fail = 0
    for i, sample in enumerate(holdout):
        meta = sample.get("meta") or {}
        nl = str(meta.get("nl") or "").strip()
        app_info = meta.get("app_info") or {}
        try:
            ref = json.loads(sample["messages"][1]["content"])
            ref_steps = len(ref.get("steps") or [])
        except (KeyError, IndexError, ValueError):
            parse_fail += 1
            rows.append({"i": i, "verdict": "bad-sample"})
            continue
        if not nl:
            parse_fail += 1
            rows.append({"i": i, "verdict": "no-nl"})
            continue
        try:
            case = client.generate(nl, app_info)
        except GenError as exc:
            msg = str(exc)
            if "请求失败" in msg or "不可达" in msg:
                return {"ok": False,
                        "error": f"本地端点 {base_url} 不可用：{msg}（先起 mlx_lm.server）"}
            parse_fail += 1
            rows.append({"i": i, "verdict": "gen-error", "error": msg[:200]})
            continue
        gap = abs(len(case.steps) - ref_steps) / max(ref_steps, 1)
        verdict = "near" if gap < 0.5 else "diverged"
        if verdict == "near":
            ok += 1
        else:
            diverged += 1
        rows.append({"i": i, "verdict": verdict, "steps": len(case.steps),
                     "ref_steps": ref_steps})
    total = len(holdout)
    return {"ok": True, "total": total, "dsl_ok": ok + diverged, "near": ok,
            "diverged": diverged, "parse_fail": parse_fail,
            "auto_pass": round((ok + diverged) / total, 3) if total else 0.0,
            "rows": rows}


def eval_adapter(root: Path, adapter: str, base: str, holdout: list[dict],
                 base_url: str = "http://127.0.0.1:8080", timeout: int = 180,
                 baseline_url: str | None = None,
                 report_path: Path | None = None) -> dict:
    """`mat finetune eval`：留出集走本地端点，与 gen 同口径生成。

    自动初筛 = DSL 合法（GenClient 的校验/后处理全部生效）；步数偏差 ≥50% 标 diverged
    供人工优先复核。`baseline_url` 给出时再跑一遍**不加载 adapter**的基座端点，
    报告里带 auto_pass 差（文档 §4.3 要求的"与基线对比"）。
    """
    outcome = _run_holdout(root, base, holdout, base_url, timeout)
    if not outcome.get("ok"):
        return outcome
    report = {
        "schema": "mat.finetune.eval/1",
        "adapter": adapter,
        "base": base,
        "endpoint": base_url,
        "total": outcome["total"],
        "dsl_ok": outcome["dsl_ok"],
        "near": outcome["near"],
        "diverged": outcome["diverged"],
        "parse_fail": outcome["parse_fail"],
        "auto_pass": outcome["auto_pass"],
        "rows": outcome["rows"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "自动初筛只证明「输出是合法 MAT DSL」——可用性人工判（工具链不背书）",
    }
    if baseline_url:
        baseline = _run_holdout(root, base, holdout, baseline_url, timeout)
        if baseline.get("ok"):
            report["baseline"] = {k: baseline[k] for k in
                                  ("total", "dsl_ok", "near", "diverged",
                                   "parse_fail", "auto_pass")}
            report["baseline"]["endpoint"] = baseline_url
            if baseline["total"]:
                report["delta_pp"] = round(
                    (outcome["auto_pass"] - baseline["auto_pass"]) * 100, 1)
        else:
            report["baseline_error"] = baseline.get("error")
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
        report["report_path"] = str(path)
    return {"ok": True, **report}


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
