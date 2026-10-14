"""MCP server 测试（无需 MCP SDK 也能测的部分：工作区解析/工具注册/越权防护）。"""
from pathlib import Path

import pytest

from insta_engine.mcp_server import build_server, resolve_workspace


def test_resolve_workspace_three_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("MAT_WORKSPACE", raising=False)
    # ① 当前目录非工作区 → 回退 ~/.mat/workspace
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace(None) == (Path.home() / ".mat" / "workspace").resolve()
    # ② 当前目录已是工作区（有 cases/）→ 用它
    (tmp_path / "cases").mkdir()
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace(None) == tmp_path.resolve()
    # ③ 显式指定优先
    assert resolve_workspace(str(tmp_path / "custom")) == (tmp_path / "custom").resolve()


def test_build_server_registers_eleven_tools(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    # root 用 tmp_path：不把仓库真实 .env 装进测试进程（否则 from_env() 拿到真 Key，测试可能打真端点花钱）
    server = build_server(tmp_path)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"mat_status", "mat_list_cases", "mat_get_case", "mat_generate_case",
            "mat_run_case", "mat_run_app", "mat_explore", "mat_job",
            "mat_cancel_job", "mat_page_graph", "mat_controls_context"} <= names


def test_mat_get_case_blocks_traversal(tmp_path):
    pytest.importorskip("mcp")
    server = build_server(tmp_path)
    # 直接调用底层 fn（call_tool 需会话；此处经 tool manager 调用实现函数）
    fn = server._tool_manager._tools["mat_get_case"].fn
    result = fn("../../.env")
    assert "error" in result


def test_mat_page_graph_exposes_titles(tmp_path):
    """`mat_page_graph` 必须**带上标题** —— docstring 一直承诺「页面清单（标题/关键元素）」，
    但实现里曾漏掉 `title`（2026-10-10 修）。

    为什么这条值钱：人工复核（`mat review`）改的就是**标题**。若 MCP 这条路不返回标题，
    那通过 MCP 写用例的 AI **看不到复核结果** ⇒ 复核在 MCP 上的收益是**零**，
    而且**看不出来**（工具照常返回一堆页面，只是没有名字）。
    """
    pytest.importorskip("mcp")
    import json

    idx = tmp_path / "artifacts" / "page_index"
    idx.mkdir(parents=True)
    (idx / "com.demo.json").write_text(json.dumps({"pages": [
        {"page_key": "k1", "depth": 0, "title": "搜索结果页", "title_source": "manual",
         "activity": "com.demo.ProductListActivity",
         "structure": {"key_elements": ["返回", "筛选"]}},
        {"page_key": "k2", "depth": 1, "title": "首页", "title_source": "ai",
         "activity": "com.demo.MainActivity", "structure": {"key_elements": ["搜索"]}},
    ]}, ensure_ascii=False), encoding="utf-8")

    server = build_server(tmp_path)
    fn = server._tool_manager._tools["mat_page_graph"].fn
    out = fn("com.demo")
    assert out["pages"] == 2
    first = out["page_list"][0]
    assert first["title"] == "搜索结果页", "标题必须返回（复核改的就是它）"
    assert first["title_source"] == "manual", "来源也要带上：调用方要能分辨人工/AI"
    assert out["page_list"][1]["title"] == "首页"


def test_mat_page_graph_missing_graph_is_an_error(tmp_path):
    pytest.importorskip("mcp")
    server = build_server(tmp_path)
    fn = server._tool_manager._tools["mat_page_graph"].fn
    assert "error" in fn("com.nope")


def test_cmd_mcp_cli_entry_actually_starts(tmp_path, monkeypatch):
    """`mat mcp --root X` 必须真的能起来 —— 回归「入口传参而 main 不收」那个死 bug。

    2026-10-06 实测：`cmd_mcp` 调 `mcp_main(args.root)`，而 `main()` 不收参数
    ⇒ **`mat mcp` 直接 TypeError，从来就没跑起来过**。

    ⚠️ 为什么原有的 3 条测试都没发现：它们**只测 `build_server()`（库层）**，
    工具注册全绿，而**用户真正敲的那条命令是死的**。
    ⇒ 这个用例刻意**从 CLI 那一端进**（`cmd_mcp`），把「入口能跑」也钉住。
    """
    pytest.importorskip("mcp")
    from insta_engine import __main__ as cli
    from insta_engine import mcp_server

    calls: dict = {}

    class _FakeServer:
        def run(self, transport: str | None = None) -> None:
            calls["transport"] = transport

    monkeypatch.setattr(mcp_server, "build_server", lambda root: _FakeServer())

    class _Args:
        root = str(tmp_path)

    assert cli.cmd_mcp(_Args()) == 0, "mat mcp 入口不应抛异常（修复前是 TypeError）"
    assert calls["transport"] == "stdio", "必须走 stdio（AI 工具接入用的传输）"


def test_mcp_main_accepts_explicit_root(tmp_path, monkeypatch):
    """`main(root)` 的签名契约：显式传 root 时**不再解析 argv**（CLI 子命令路径）。"""
    pytest.importorskip("mcp")
    from insta_engine import mcp_server

    seen: dict = {}

    class _FakeServer:
        def run(self, transport: str | None = None) -> None:
            seen["transport"] = transport

    monkeypatch.setattr(mcp_server, "build_server", lambda root: _FakeServer())
    # 若 main 仍去解析 argv，pytest 的参数会被当成未知参数而报错
    mcp_server.main(str(tmp_path))
    assert seen["transport"] == "stdio"


def test_cmd_mcp_without_root_does_not_choke_on_cli_argv(tmp_path, monkeypatch):
    """`mat mcp`（**不带** `--root`）必须也能起 —— 回归「整体 parse_args 撞上 CLI 子命令名」。

    实测：修掉第一个 bug（入口传参而 main 不收）之后，不带 `--root` 仍然挂——
    因为走到 `parse_args()` 会去解析 `sys.argv`，而经 `mat mcp` 调用时
    `sys.argv` 里**还留着 CLI 自己的子命令名 `mcp`** ⇒ `unrecognized arguments: mcp`。
    ⇒ 必须用哨兵判断**是谁在调用**，而不是看 root 是否为 None。
    """
    pytest.importorskip("mcp")
    import sys

    from insta_engine import __main__ as cli
    from insta_engine import mcp_server

    monkeypatch.setenv("MAT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["/path/to/mat", "mcp"])  # 真实调用时的样子

    class _FakeServer:
        def run(self, transport: str | None = None) -> None:
            pass

    monkeypatch.setattr(mcp_server, "build_server", lambda root: _FakeServer())

    class _Args:
        root = None  # 用户没写 --root

    assert cli.cmd_mcp(_Args()) == 0, "不带 --root 也必须能起（修复前报 unrecognized arguments: mcp）"


def test_mat_mcp_console_script_still_parses_its_own_argv(tmp_path, monkeypatch):
    """另一条入口不能修坏：`mat-mcp --root X` 仍要自己解析 argv（哨兵路径）。"""
    pytest.importorskip("mcp")
    import sys

    from insta_engine import mcp_server

    monkeypatch.setattr(sys, "argv", ["mat-mcp", "--root", str(tmp_path)])
    seen: dict = {}

    class _FakeServer:
        def run(self, transport: str | None = None) -> None:
            seen["transport"] = transport

    monkeypatch.setattr(mcp_server, "build_server", lambda root: _FakeServer())
    mcp_server.main()  # 不传参 ⇒ 走 _UNSET 分支
    assert seen["transport"] == "stdio"



