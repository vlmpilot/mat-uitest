"""`mat suite` —— 套件（测试项目）管理。

**为什么要有这个命令**：`mat run` 早就能跑套件 JSON（`{"name":..., "cases":[...]}`），
但套件**没有管理者**——不能列、不能加、不能查，只能手写 JSON。团队共管一个 App 时
「这个项目有哪几条用例 / 加一条 / 校验有没有写错」全靠人记。

**设计约束（都是实测出来的，不是想象的）**：
- 套件文件放 `cases/<name>.suite.json`。`mat run cases/ --app <pkg>` 的批量过滤读
  `target.package`，套件没有 `target` ⇒ **不会被误当成用例跑**（`__main__.py:210-212`）。
  `web_server.py` 的删除守卫也按 `.suite.json` 识别，两者同名不会打架。
- 用例是**纯 JSON + 已入库**（未被 .gitignore）⇒ 套件进 git 后团队用 PR review 就能协作，
  不需要任何服务端。这是最小可行协作形态。
- 套件里可以放**内联用例**（`{"caseId": ...}` 字典），本命令只管理**文件引用**这一种，
  读的时候原样保留内联项不动——不擅自改别人的写法。
- **条目统一纯文件名**（`c0.json`，不是 `cases/c0.json`）：套件的三个写者——配置台
  api_suite_save、web 批量运行临时套件、本命令——必须同构，否则 check 对配置台套件
  全量误报「文件不存在」。run 按「套件所在目录/条目」解析（manifest 语义），纯文件名
  不依赖 CWD 最健壮；历史文件里的 `cases/` 前缀读时归一化兼容（见 `_norm_ref`）。

**不做的事**：不改 `run` 的执行语义、不引入依赖、不碰 `~/.mat`。
"""

from __future__ import annotations

import json
from pathlib import Path

from .dsl import TestCase

SUITE_SUFFIX = ".suite.json"


# ---- 路径与读写 ----------------------------------------------------------------

def suites_dir(root: Path) -> Path:
    return Path(root) / "cases"


def suite_path(root: Path, name: str) -> Path:
    return suites_dir(root) / f"{name}{SUITE_SUFFIX}"


def _validate_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"套件名非法: {name!r}（不能含路径分隔符，也不能以 . 开头）")
    return name


def _flat_cases(data: dict) -> list:
    """平铺取用例条目：cases 写法直读；stages 写法按阶段顺序摊平（2026-10-21）。"""
    if isinstance(data.get("cases"), list):
        return list(data["cases"])
    flat: list = []
    for st in data.get("stages") or []:
        if isinstance(st, dict) and isinstance(st.get("cases"), list):
            flat.extend(st["cases"])
    return flat


def read_suite(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not ("cases" in data or "stages" in data):
        raise ValueError(f"{path} 不是套件（应含 cases 或 stages 数组）")
    if not _flat_cases(data):
        raise ValueError(f"套件 {path} 没有用例（cases / stages 均为空）")
    return data


def write_suite(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_suites(root: Path) -> list[tuple[str, Path]]:
    d = suites_dir(root)
    if not d.exists():
        return []
    return sorted((p.name[: -len(SUITE_SUFFIX)], p) for p in d.glob(f"*{SUITE_SUFFIX}"))


# ---- 引用解析 ----------------------------------------------------------------

def _norm_ref(ref: str) -> str:
    """条目归一化：统一成**纯文件名**。

    接受两种写法：纯文件名（`c0.json`，配置台/临时套件口径）与历史 `cases/` 前缀
    （`cases/c0.json`，本命令旧版写出）。其余——子目录、`..`、绝对路径——一律拒绝：
    套件与用例同层平铺是配置台与删除守卫的共同前提，写法上的「灵活」只会制造
    双入口不同构。
    """
    name = Path(ref).name
    prefix_ok = ref == name or ref == f"cases/{name}"
    if not prefix_ok or name in ("", ".", "..") or name.endswith(SUITE_SUFFIX):
        raise ValueError(
            f"套件条目写法不支持: {ref!r}（应为 cases/ 下的用例文件名，如 c0.json）")
    return name


def expand_refs(root: Path, refs: list[str]) -> list[str]:
    """把命令行给的路径参数展开成**纯文件名**（与配置台 api_suite_save 同构）。

    支持三种写法：直接给文件名（相对 cases/）、给 cases/x.json、给 glob。
    去重但**保序**——套件里的顺序就是执行顺序，不能乱。
    只在 cases/ 平铺层搜索；引用 cases/ 外的文件不属于套件的能力范围
    （配置台同样只接受 cases/ 下用例），要走这路的先把用例挪进 cases/。
    """
    out: list[str] = []
    seen: set[str] = set()
    cases = suites_dir(root)
    for ref in refs:
        matched: list[Path] = []
        if any(ch in ref for ch in "*?["):
            matched = sorted(cases.glob(Path(ref).name))
        else:
            cand = cases / Path(ref).name
            if cand.exists():
                matched = [cand]
        usable = [m for m in matched if not m.name.endswith(SUITE_SUFFIX)]
        if not matched:
            raise FileNotFoundError(f"找不到用例: {ref}（在 {cases} 下找）")
        if not usable:
            raise FileNotFoundError(
                f"{ref} 只匹配到套件文件（{SUITE_SUFFIX}）——套件不能被当成用例加进另一个套件")
        for m in matched:
            # `mat suite add x '*.json'` 会把套件文件自己匹配进来（实测踩过：
            # check 随后报 "DSL 不合法: cases/x.suite.json"）。套件不是用例，排除掉。
            if m.name.endswith(SUITE_SUFFIX):
                continue
            if m.name not in seen:
                seen.add(m.name)
                out.append(m.name)
    return out


def refs_of(data: dict) -> tuple[list[str], list[dict]]:
    """拆出（文件引用列表, 内联用例列表）。内联项原样保留。"""
    files: list[str] = []
    inline: list[dict] = []
    for item in _flat_cases(data):
        if isinstance(item, str):
            files.append(item)
        elif isinstance(item, dict):
            inline.append(item)
        else:
            raise ValueError(f"cases 里的元素只能是路径字符串或内联用例字典，收到 {type(item).__name__}")
    return files, inline


# ---- 各子命令 ----------------------------------------------------------------

def cmd_list(args) -> int:
    root = Path(args.root)
    suites = list_suites(root)
    if not suites:
        print(f"工作区 {root} 下还没有套件（{suites_dir(root)}/*.suite.json）")
        print("建一个：mat suite new <名称>")
        return 0
    print(f"工作区 {root} 共 {len(suites)} 个套件：\n")
    for name, path in suites:
        try:
            data = read_suite(path)
            files, inline = refs_of(data)
            print(f"  {name:<20} {len(data['cases']):>3} 条（{len(files)} 文件 + {len(inline)} 内联）  {path}")
        except ValueError as exc:
            print(f"  {name:<20} ⚠️  读不出来：{exc}")
    return 0


def cmd_new(args) -> int:
    root = Path(args.root)
    name = _validate_name(args.name)
    path = suite_path(root, name)
    if path.exists() and not args.force:
        print(f"套件已存在：{path}（要覆盖加 --force）")
        return 1
    refs = expand_refs(root, args.add) if args.add else []
    if not refs and not args.allow_empty:
        print("空套件不能跑。建的时候用 --add <用例> 带上用例，或显式 --allow-empty。")
        return 1
    write_suite(path, {"name": name, "cases": refs})
    print(f"已建 {path}（{len(refs)} 条）")
    if refs:
        print(f"跑它：mat run cases/{name}{SUITE_SUFFIX}")
    return 0


def _load_or_die(root: Path, name: str) -> tuple[Path, dict]:
    path = suite_path(root, name)
    if not path.exists():
        print(f"没有套件 {name}（找 {path}）")
        print(f"现有的：{', '.join(n for n, _ in list_suites(root)) or '（无）'}")
        raise SystemExit(1)
    return path, read_suite(path)


def cmd_add(args) -> int:
    root = Path(args.root)
    _, data = _load_or_die(root, args.name)
    if "stages" in data and "cases" not in data:
        print("分阶段套件（stages）请直接编辑文件——mat suite add 暂只支持平铺 cases 写法")
        return 1
    files, _ = refs_of(data)
    files_norm = [_norm_ref(f) for f in files]  # 历史前缀条目归一化，避免判重失配
    new = expand_refs(root, args.cases)
    added = [r for r in new if r not in files_norm]
    if not added:
        print("都在套件里了，没有新增。")
        return 0
    data["cases"] = files_norm + added
    write_suite(suite_path(root, args.name), data)
    print(f"已加入 {len(added)} 条：")
    for r in added:
        print(f"  + {r}")
    return 0


def cmd_rm(args) -> int:
    root = Path(args.root)
    _, data = _load_or_die(root, args.name)
    if "stages" in data and "cases" not in data:
        print("分阶段套件（stages）请直接编辑文件——mat suite rm 暂只支持平铺 cases 写法")
        return 1
    files, inline = refs_of(data)
    # 归一化比较 + 落盘即迁移：历史 `cases/` 前缀条目重写为纯文件名（与配置台同构）
    files_norm = [_norm_ref(f) for f in files]
    targets = [expand_refs(root, [c])[0] for c in args.cases]
    missing = [t for t in targets if t not in files_norm]
    if missing:
        print(f"这些不在套件里：{', '.join(missing)}")
    kept = [f for f in files_norm if f not in targets]
    if len(kept) == len(files_norm):
        print("没有变化。")
        return 1 if missing else 0
    if not kept and not inline:
        print("移除后套件空了。套件不能为空——请直接删文件：rm " + str(suite_path(root, args.name)))
        return 1
    data["cases"] = kept + inline
    write_suite(suite_path(root, args.name), data)
    print(f"已移除 {len(files_norm) - len(kept)} 条，剩 {len(kept)} 条。")
    return 0


def cmd_show(args) -> int:
    root = Path(args.root)
    _, data = _load_or_die(root, args.name)
    if "stages" in data and "cases" not in data:
        print(f"套件 {data.get('name') or args.name}（分阶段写法，执行顺序即阶段顺序）\n")
        for st in data.get("stages") or []:
            gate = st.get("on_failure") or data.get("on_failure") or "continue"
            print(f"  ▸ {st.get('name') or '(未命名)'}  on_failure={gate}")
            for i, item in enumerate(st.get("cases") or [], 1):
                label = item if isinstance(item, str) else (item.get("caseId") or "(内联用例)")
                print(f"    {i:>2}. {label}")
        return 0
    print(f"套件 {data.get('name') or args.name}（{len(data['cases'])} 条，执行顺序即下列顺序）\n")
    for i, item in enumerate(data["cases"], 1):
        if isinstance(item, str):
            try:
                rel = _norm_ref(item)
            except ValueError as exc:
                print(f"  {i:>2}. {item}  ⚠️  {exc}")
                continue
            p = suites_dir(root) / rel
            mark = "✅" if p.exists() else "❌ 文件不存在"
            cid = pkg = name = ""
            if p.exists():
                try:
                    c = TestCase.model_validate(json.loads(p.read_text(encoding="utf-8")))
                    cid, pkg, name = c.case_id, (c.target or {}).get("package", ""), (c.name or "")
                except Exception as exc:  # noqa: BLE001 — 校验失败要报出来，不是崩掉
                    mark = f"⚠️  DSL 不合法：{str(exc)[:60]}"
            print(f"  {i:>2}. {rel}  {mark}")
            if cid:
                print(f"      {cid}{'  ' + name if name else ''}{'  ' + pkg if pkg else ''}")
        else:
            print(f"  {i:>2}. （内联用例）{item.get('caseId', '?')}")
    return 0


def cmd_check(args) -> int:
    """校验：文件在不在、DSL 合不合法、包名是否一致。

    跨 App 混跑是套件最常见的手写错误——它不会报错，只会在跑到一半时因为设备/目标
    不匹配而失败，所以在这里提前拦。
    """
    root = Path(args.root)
    _, data = _load_or_die(root, args.name)
    files, inline = refs_of(data)
    problems: list[str] = []
    packages: dict[str, list[str]] = {}

    for raw in files:
        try:
            rel = _norm_ref(raw)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        p = suites_dir(root) / rel
        if not p.exists():
            problems.append(f"文件不存在: {rel}")
            continue
        try:
            c = TestCase.model_validate(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"DSL 不合法: {rel} — {str(exc)[:80]}")
            continue
        pkg = (c.target or {}).get("package") or ""
        packages.setdefault(pkg, []).append(rel)

    for item in inline:
        try:
            TestCase.model_validate(item)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"内联用例不合法: {str(item)[:40]} — {str(exc)[:60]}")

    print(f"套件 {args.name}：{len(files)} 条文件引用 + {len(inline)} 条内联")
    if packages:
        print("按被测包名：")
        for pkg, refs in sorted(packages.items()):
            print(f"  {pkg or '(未声明 package)':<34} {len(refs)} 条")
    if len(packages) > 1:
        problems.append("套件里混了多个包名——`mat run` 只在一部设备上跑，跨 App 建议拆成多个套件")

    if problems:
        print("\n有问题：")
        for p_ in problems:
            print(f"  ✗ {p_}")
        return 1
    print("\n✅ 校验通过，可以 mat run")
    return 0
