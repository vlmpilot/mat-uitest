"""MAT MCP 服务器（工作流集成）：把 MAT 的能力暴露为 MCP 工具。

让 Cursor / Codex / Claude Desktop 等 AI 编码工具能直接驱动 MAT——
在 IDE 里跑用例、生成用例、看事件流、查页面图谱，而不必切到配置台。

形态：stdio 长驻进程（由 MCP 客户端拉起），复用 web_server 的全部 api_* 函数
（鉴权无关：MCP 进程与用户同机同权限，本就"仅本机监听"的同一信任域）。

工作区解析与 `mat console` 相同规则（防产物散落）：
MAT_WORKSPACE 环境变量 > --root 参数 > 当前目录（若是工作区）> ~/.mat/workspace。

安装：pip install 'mat-uitest[mcp]'；接入：。

**生命周期边界**：stdio 服务器进程随 MCP 客户端会话生灭。Cursor/Codex 全会话常驻，
任务在 IDE 打开期间持续运行；但**关闭 IDE/会话会中止进行中的任务**（内存任务注册表
随进程消失）。超长任务请用 `mat console` 提交（独立进程不随会话消亡）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .state import state_home


def resolve_workspace(explicit: str | None) -> Path:
    """与 mat console 相同的规则：显式 > 环境变量 > 当前目录（若是工作区）> ~/.mat/workspace。"""
    if explicit:
        root = Path(explicit).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    env = os.environ.get("MAT_WORKSPACE")
    if env:
        root = Path(env).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    cwd = Path.cwd()
    if (cwd / "cases").exists() or (cwd / "artifacts").exists():
        return cwd
    root = state_home() / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load_workspace_env(root: Path) -> None:
    """加载工作区 .env（GEN_* 等），已设置的环境变量不覆盖——与 CLI/console 同语义。"""
    from .__main__ import _load_env_file

    _load_env_file(root / ".env")


def build_server(root: Path):
    from mcp.server.mcpserver import MCPServer

    from . import web_server as ws
    from .__main__ import _load_env_file

    _load_env_file(root / ".env")
    mcp = MCPServer("MAT")

    @mcp.tool()
    def mat_status() -> dict:
        """工作区总览：在线设备、用例列表、AI 配置状态、本月配额、最近执行与探索。"""
        code, obj = ws.api_state(root)
        return obj if code == 200 else {"error": obj}

    @mcp.tool()
    def mat_list_cases() -> dict:
        """列出工作区全部测试用例（名称、被测应用）。"""
        code, obj = ws.api_state(root)
        if code != 200:
            return {"error": obj}
        return {"cases": [{"name": n, "app": (obj.get("caseMeta") or {}).get(n, {}).get("app", "—")}
                          for n in obj.get("cases", [])]}

    @mcp.tool()
    def mat_get_case(name: str) -> dict:
        """读取某个用例的完整 DSL JSON（步骤/断言/变量）。"""
        target = root / "cases" / name
        if "/" in name or ".." in name or not target.exists():
            return {"error": f"用例不存在: {name}"}
        return json.loads(target.read_text(encoding="utf-8"))

    @mcp.tool()
    def mat_generate_case(nl: str, package: str) -> dict:
        """用一句话自然语言生成测试用例（消耗 1 次 AI 生成额度，结果写入 cases/）。

        建议先对被测 App 执行过 mat index/explore（有页面图谱时生成更准）。
        返回 job_id，用 mat_job 查进度与结果文件名。
        """
        code, obj = ws.api_gen(root, {"nl": nl, "package": package})
        return obj if code == 200 else {"error": obj}

    @mcp.tool()
    def mat_run_case(case: str, serial: str | None = None,
                     platform: str = "android") -> dict:
        """在设备上运行一个测试用例（后台任务；每步截图证据、失败自动分类）。

        返回 job_id，用 mat_job 查事件流与报告路径。传 serial 指定设备，缺省取唯一在线设备。
        platform="android"（默认）或 "ios"（模拟器 MVP，需 WDA 在 localhost:8100）。
        """
        code, obj = ws.api_run(root, {"case": case, "serial": serial, "platform": platform})
        return obj if code == 200 else {"error": obj}

    @mcp.tool()
    def mat_run_app(app: str, serial: str | None = None,
                    platform: str = "android") -> dict:
        """批量运行某被测应用下的全部用例（回归套件）。返回 job_id。

        platform="android"（默认）或 "ios"（模拟器 MVP）。
        """
        code, obj = ws.api_run(root, {"app": app, "serial": serial, "platform": platform})
        return obj if code == 200 else {"error": obj}

    @mcp.tool()
    def mat_explore(package: str, minutes: float = 2, steps: int = 30,
                    max_depth: int = 3, platform: str = "android") -> dict:
        """启动探索测试：规则驱动自主遍历 App，发现崩溃/白屏并沉淀页面图谱。

        返回 job_id，用 mat_job 查进度；报告在 artifacts/explore/<批次>/report.html。
        仅 Android：iOS 的页面图谱适配在 P3（传 ios 会被诚实拒绝）。
        """
        code, obj = ws.api_explore(root, {"package": package, "minutes": minutes,
                                          "steps": steps, "max_depth": max_depth,
                                          "platform": platform})
        return obj if code == 200 else {"error": obj}

    @mcp.tool()
    def mat_job(job_id: str, since: int = 0) -> dict:
        """查询任务进度：状态、摘要、增量步骤事件（since=已见过的最大 seq）。"""
        code, obj = ws.api_job_events({"id": job_id, "since": since})
        if code != 200:
            return {"error": "任务不存在或已随上次 MCP 会话结束——"
                    "长任务请用 mat console 提交（独立于本进程存活）"}
        jobs = ws.api_jobs_list()[1]["jobs"]
        info = next((j for j in jobs if j["id"] == job_id), None)
        return {**obj, "summary": info.get("summary") if info else None,
                "link": info.get("link") if info else None}

    @mcp.tool()
    def mat_cancel_job(job_id: str) -> dict:
        """请求中止一个运行中的任务（当前步完成后安全退出，已完成证据保留）。"""
        code, obj = ws.api_job_cancel({"id": job_id})
        return obj if code == 200 else obj

    @mcp.tool()
    def mat_page_graph(package: str) -> dict:
        """读取被测 App 的页面图谱摘要：层级分布、页面清单（标题/关键元素）、导航边。

        用于在写用例或改代码前了解 App 结构（探索测试与 mat index 沉淀）。
        """
        from .page_index import load_index

        # A3 同口径（2026-10-12 评审）：包名直接拼 artifacts 路径，形状非法即拒。
        from .pkgname import safe_package_name

        try:
            safe_package_name(package)
        except ValueError as exc:
            return {"error": str(exc)}
        index = load_index(root / "artifacts" / "page_index" / f"{package}.json")
        if not index:
            return {"error": "该应用暂无页面图谱（先 mat index <包名> 或 mat explore）"}
        pages = index.get("pages", [])
        depth: dict[str, int] = {}
        for p in pages:
            d = p.get("depth")
            if d is not None:
                depth[f"L{d}"] = depth.get(f"L{d}", 0) + 1
        return {
            "package": package, "pages": len(pages), "depth_stats": depth,
            "edges": len(index.get("edges", [])),
            # ⚠️ `title` / `title_source` 必须带上（2026-10-10 修）：docstring 一直写着
            # 「页面清单（标题/关键元素）」，但实现里**漏了 title** ⇒ 通过 MCP 写用例的 AI
            # **看不到人工复核改过的页名**，复核的收益在 MCP 这条路上是零。
            "page_list": [{"depth": p.get("depth"),
                           "title": p.get("title") or "",
                           "title_source": p.get("title_source") or "",
                           "activity": p.get("activity", "")[:60],
                           "key_elements": (p.get("structure") or {}).get("key_elements", [])[:4]}
                          for p in pages][:30],
        }

    @mcp.tool()
    def mat_controls_context(package: str) -> str:
        """读取被测 App 的控件清单（res-id + 可见文案，防幻觉底座）。

        写用例/测试脚本时应只引用此清单中真实存在的控件。
        """
        from .page_index import controls_context_for

        return controls_context_for(package, root / "artifacts" / "page_index") or \
            "（该应用暂无控件索引——先 mat index <包名>）"

    return mcp


#: 哨兵：区分「console script 自己解析 argv」与「CLI 子命令已经解析好」。
#: 不能用 `None` 当哨兵——`mat mcp` 不带 `--root` 时 `args.root` 本来就是 None。
_UNSET: object = object()


def main(root: str | None | object = _UNSET) -> None:
    """MCP 服务器入口。**两条入口都必须能用**：

    · `mat-mcp --root X`（console script）—— 自己解析 argv；
    · `mat mcp [--root X]`（CLI 子命令）—— **root 由 CLI 解析好传进来**，此处**绝不能再解析 argv**。

    ⚠️ 这里踩过**两个**坑（都是 2026-10-06 实测出来的，此前 `mat mcp` 从来跑不起来）：

    1. 本函数原本不收参数，而 `cmd_mcp` 传了 `args.root`
       ⇒ `TypeError: main() takes 0 positional arguments but 1 was given`。
    2. 修掉①后，**不带 `--root`** 仍挂：走到 `parse_args()` 会去解析 `sys.argv`，
       而经 `mat mcp` 调用时 `sys.argv` 里**还留着 CLI 自己的子命令名 `mcp`**
       ⇒ `error: unrecognized arguments: mcp`。
       ⇒ 所以必须用哨兵判断**是谁在调用**，而不是看 root 是否为 None。

    之所以一直没发现：**单测只测 `build_server()`（库层）**，工具注册全绿，
    而用户真正敲的那条命令是死的。
    """
    if root is _UNSET:
        ap = argparse.ArgumentParser(prog="mat-mcp", description="MAT MCP 服务器（stdio）")
        ap.add_argument("--root", default=None, help="工作区根目录（缺省规则同 mat console）")
        root = ap.parse_args().root

    try:
        from importlib.util import find_spec

        if find_spec("mcp") is None:
            raise ModuleNotFoundError("mcp")
    except ModuleNotFoundError:
        print("MCP SDK 未安装：pip install 'mat-uitest[mcp]'", file=sys.stderr)
        raise SystemExit(1) from None

    root = resolve_workspace(root if isinstance(root, str) else None)
    _load_workspace_env(root)

    # 复用 build_server（含工具注册）；MCPServer 实例在其中构造
    server = build_server(root)
    print(f"MAT MCP 就绪 · 工作区: {root}", file=sys.stderr)  # stderr 不污染 stdio 协议
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
