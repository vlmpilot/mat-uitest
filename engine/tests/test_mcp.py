"""MCP server 测试（无需 MCP SDK 也能测的部分：工作区解析/工具注册/越权防护）。"""

import json

import pytest

from insta_engine.mcp_server import build_server, resolve_workspace


def test_resolve_workspace_three_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("MAT_WORKSPACE", raising=False)
    monkeypatch.setenv("MAT_HOME", str(tmp_path / "mat"))  # 隔离兜底播种，别写进真实 home
    # ① 当前目录非工作区 → 回退默认工作区，且自动铺到开箱即用（示例用例 + prompt 模板）
    monkeypatch.chdir(tmp_path)
    root = resolve_workspace(None)
    assert root == (tmp_path / "mat" / "workspace").resolve()
    assert (root / "cases" / "example-settings.json").exists(), "默认工作区必须自动备好示例用例（免 mat init）"
    assert (root / "artifacts").is_dir()
    assert (root / "prompts" / "gen.prompt").exists(), "prompt 模板必须自动播种"
    # ①' 幂等：已存在的文件不被覆盖（用户改过也不许动）
    (root / "cases" / "example-settings.json").write_text("{}", encoding="utf-8")
    resolve_workspace(None)
    assert (root / "cases" / "example-settings.json").read_text(encoding="utf-8") == "{}"
    # ② 当前目录已是工作区（有 cases/）→ 用它，且绝不往用户目录里塞文件
    (tmp_path / "cases").mkdir()
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace(None) == tmp_path.resolve()
    assert not (tmp_path / "prompts").exists(), "用户自己的工作区不得代铺"
    # ③ 显式指定优先，同样不播种
    custom = resolve_workspace(str(tmp_path / "custom"))
    assert custom == (tmp_path / "custom").resolve()
    assert not (custom / "cases").exists()


def test_build_server_registers_all_tools(tmp_path):
    """工具清单是**对外承诺**（首页写着「N 个 MCP 工具」），少一个就是文档说谎。"""
    pytest.importorskip("mcp")
    import asyncio

    # root 用 tmp_path：不把仓库真实 .env 装进测试进程（否则 from_env() 拿到真 Key，测试可能打真端点花钱）
    server = build_server(tmp_path)
    names = {t.name for t in asyncio.run(server.list_tools())}
    expected = {"mat_status", "mat_list_cases", "mat_get_case", "mat_generate_case",
                "mat_run_case", "mat_run_app", "mat_explore", "mat_job",
                "mat_cancel_job", "mat_page_graph", "mat_controls_context",
                "mat_verify", "mat_screenshot", "mat_doctor", "mat_cred"}
    assert expected <= names, f"缺少工具：{sorted(expected - names)}"
    assert len(names) == 15, f"工具数变了（{len(names)}）——同步首页的「15 个 MCP 工具」"


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





def test_mat_verify_output_passes_own_schema(tmp_path, monkeypatch):
    """MCP 的 `mat_verify` 返回体必须能被**我们自己发布的** schema 校验通过。

    回归锁（评审 P1-3）：曾在返回前塞 `mcp_exit_code`，而模型是 `extra="forbid"`、
    schema 是 `additionalProperties: false` ⇒ agent 拿我们发布的 schema 校验 MCP
    响应会判非法。`exit_code` 本来就在 verdict 里，那个键是纯冗余。
    """
    pytest.importorskip("mcp")
    import json

    from insta_engine import verify as verify_mod
    from insta_engine.verify_models import VerifyVerdict

    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "a.json").write_text(
        json.dumps({"caseId": "a", "steps": [{"id": "s1", "action": "launch"}]}),
        encoding="utf-8")
    canned = {"schema": "mat.verify/1", "verdict": "fail", "ok": False, "exit_code": 2,
              "case_id": "a", "steps": {"total": 1, "passed": 0},
              "next_actions": [{"category": "locate-fail", "action": "看控件树"}]}
    monkeypatch.setattr(verify_mod, "run_verify", lambda *a, **k: (2, dict(canned)))

    server = build_server(tmp_path)
    tool = server._tool_manager.get_tool("mat_verify")
    out = tool.fn("a.json")

    assert "mcp_exit_code" not in out, "协议外的字段会让 schema 校验失败"
    VerifyVerdict.model_validate(out)  # 多余键/缺必填都会在这里抛


# -- 终检接缝 #1：环境自检结构化 ------------------------------------------------

def test_mat_doctor_returns_structured_checks(tmp_path, monkeypatch):
    """agent 开工前自检要 JSON，不是人看的终端文本。"""
    pytest.importorskip("mcp")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))

    server = build_server(tmp_path)
    out = server._tool_manager.get_tool("mat_doctor").fn(platform="android")

    assert isinstance(out.get("ok"), bool)
    assert out["checks"], "至少要返回检查项——空列表等于什么都没检"
    for c in out["checks"]:
        assert {"id", "label", "status"} <= set(c), c
    # 阻塞/警告是 checks 的子集，且不是另一套口径
    ids = {c["id"] for c in out["checks"]}
    assert {c["id"] for c in out["blocking"]} <= ids
    assert {c["id"] for c in out["warnings"]} <= ids
    assert out["ok"] is (not out["blocking"])


def test_mat_doctor_rejects_unknown_platform(tmp_path, monkeypatch):
    """拼错平台要报错，不能静默按「全平台」检一遍。

    回归锁（2026-11-01 复核）：`doctor.run()` 对未知取值走 `return android+ios+harmony`
    （`doctor.py:696`）⇒ 透传时 `platform="foo"` 返回 18 条检查（android 只有 8 条），
    `ok` 把 iOS/harmony 的阻塞项也算进去——agent 在自己的平台上找不到对应问题。
    """
    pytest.importorskip("mcp")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))

    fn = build_server(tmp_path)._tool_manager.get_tool("mat_doctor").fn
    for bad in ("foo", "Android2", "harmonyos"):
        out = fn(platform=bad)
        assert "error" in out, f"{bad} 不该被当成合法平台：{out}"
        assert "android" in out["error"], "报错要给出可选值，否则 agent 只能猜"
    assert fn(platform="ANDROID")["platform"] == "android", "大小写不该是错误来源"


# -- 终检接缝 #2：凭据的非交互入口 ----------------------------------------------

def test_mat_cred_never_returns_values(tmp_path, monkeypatch):
    """MCP 的凭据工具只回名称/状态，**永远不回值**。"""
    pytest.importorskip("mcp")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))

    tool = build_server(tmp_path)._tool_manager.get_tool("mat_cred")
    assert tool.fn("set", "u1", "p@ss")["saved"] == "u1"
    got = tool.fn("list")
    assert got == {"names": ["u1"]}, f"列表只能有名字：{got}"
    assert tool.fn("delete", "u1")["deleted"] is True


def test_mat_cred_rejects_missing_fields(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))

    tool = build_server(tmp_path)._tool_manager.get_tool("mat_cred")
    assert "error" in tool.fn("set", "u1", None)
    assert "error" in tool.fn("delete", "", None)
    assert "error" in tool.fn("nonsense", "u1", None)


# -- 终检接缝 #3：批量子集走 MCP -----------------------------------------------

def test_mat_verify_subset_returns_aggregate(tmp_path, monkeypatch):
    """`mat_verify(subset=...)` 一次调用拿聚合结论，而不是逐条调。"""
    pytest.importorskip("mcp")
    from insta_engine import verify_subset as vs
    from insta_engine.verify_models import VerifyVerdict

    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    (tmp_path / "cases").mkdir()
    for name, pkg in (("a", "com.demo.app"), ("b", "com.other.app")):
        (tmp_path / "cases" / f"{name}.json").write_text(json.dumps({
            "caseId": name, "target": {"package": pkg}, "meta": {"tags": ["smoke"]},
            "steps": [{"id": "s1", "action": "launch", "params": {"package": pkg}}],
        }), encoding="utf-8")
    monkeypatch.setattr(vs, "run_subset", lambda *a, **k: {
        "schema": "mat.verify/1", "mode": "subset", "verdict": "pass", "ok": True,
        "exit_code": 0, "summary": {"selected": 1, "passed": 1, "failed": 0,
                                    "blocked": 0, "incomplete": 0},
        "selection": {"mode": k["mode"], "package": k["package"], "considered": 1,
                      "selected": 1}, "cases": [], "next_actions": []})

    out = build_server(tmp_path)._tool_manager.get_tool("mat_verify").fn(
        subset="smoke", package="com.demo.app")

    assert out["mode"] == "subset" and out["verdict"] == "pass"
    VerifyVerdict.model_validate(out)  # 不多键、不缺必填


def test_mat_verify_needs_exactly_one_mode(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    tool = build_server(tmp_path)._tool_manager.get_tool("mat_verify")

    assert "error" in tool.fn(case="a.json", subset="smoke")
    assert "error" in tool.fn()
    assert "package" in tool.fn(subset="smoke")["error"]
