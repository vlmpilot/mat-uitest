"""`mat mcp install / uninstall` 的安全边界。

这条命令会改用户的 IDE 配置，所以判据不是"能写进去"，而是**写错的情况不能发生**：
只动自己的键、坏配置一字不改、客户端没装就不硬造目录、重复跑不重复备份。
"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine import mcp_install as mi


def _target(path: Path) -> mi.McpTarget:
    return mi.McpTarget("t", "测试客户端", path)


def test_install_creates_file_and_points_at_workspace(tmp_path):
    cfg = tmp_path / "mcp.json"
    res = mi.install(_target(cfg), tmp_path)
    assert res["ok"] and res["changed"]
    entry = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["mat"]
    assert entry["args"][0] == "mcp" and "--root" in entry["args"]
    assert str(tmp_path) in entry["args"]


def test_install_preserves_other_keys(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x", "args": []}},
                               "theme": "dark"}), encoding="utf-8")
    mi.install(_target(cfg), tmp_path)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"command": "x", "args": []}
    assert data["theme"] == "dark", "别人的键必须原样保留"
    assert "mat" in data["mcpServers"]


def test_install_is_idempotent(tmp_path):
    cfg = tmp_path / "mcp.json"
    mi.install(_target(cfg), tmp_path)
    first = cfg.read_text(encoding="utf-8")
    res = mi.install(_target(cfg), tmp_path)
    assert res["ok"] and not res["changed"]
    assert cfg.read_text(encoding="utf-8") == first


def test_install_refuses_broken_json_without_touching_it(tmp_path):
    cfg = tmp_path / "mcp.json"
    broken = '{"mcpServers": {'
    cfg.write_text(broken, encoding="utf-8")
    res = mi.install(_target(cfg), tmp_path)
    assert res["ok"] is False and "不是合法 JSON" in res["error"]
    assert cfg.read_text(encoding="utf-8") == broken, "坏配置一字都不能动"


def test_install_refuses_non_object_servers(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": []}', encoding="utf-8")
    res = mi.install(_target(cfg), tmp_path)
    assert res["ok"] is False
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"mcpServers": []}


def test_install_refuses_when_client_dir_missing(tmp_path):
    cfg = tmp_path / "not-installed" / "mcp.json"
    res = mi.install(_target(cfg), tmp_path)
    assert res["ok"] is False and "没找到" in res["error"]
    assert not cfg.parent.exists(), "客户端没装就不能硬造目录（造了它也不会读）"


def test_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / "mcp.json"
    res = mi.install(_target(cfg), tmp_path, dry_run=True)
    assert res["ok"] and res["dry_run"] and "preview" in res
    assert not cfg.exists()


def test_overwrite_makes_backup(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"mat": {"command": "old", "args": []}}}),
                   encoding="utf-8")
    res = mi.install(_target(cfg), tmp_path)
    assert res["changed"] and res["backup"]
    old = json.loads(Path(res["backup"]).read_text(encoding="utf-8"))
    assert old["mcpServers"]["mat"]["command"] == "old", "备份里必须是改之前的内容"


def test_uninstall_keeps_other_servers(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"mat": {"command": "x", "args": []},
                                              "other": {"command": "y", "args": []}}}),
                   encoding="utf-8")
    res = mi.uninstall(_target(cfg))
    assert res["ok"] and res["changed"]
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "mat" not in data["mcpServers"] and "other" in data["mcpServers"]


def test_uninstall_drops_empty_container(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"mat": {"command": "x", "args": []}}}),
                   encoding="utf-8")
    mi.uninstall(_target(cfg))
    assert "mcpServers" not in json.loads(cfg.read_text(encoding="utf-8")), \
        "摘干净后别留一个空对象"


def test_uninstall_when_absent_is_noop(tmp_path):
    cfg = tmp_path / "mcp.json"
    res = mi.uninstall(_target(cfg))
    assert res["ok"] and not res["changed"]


def test_launch_spec_always_absolute(tmp_path, monkeypatch):
    """GUI 客户端不继承登录 shell 的 PATH ⇒ command 必须绝对路径。"""
    monkeypatch.setattr(mi.shutil, "which", lambda _name: None)
    spec = mi.launch_spec(tmp_path)
    assert Path(spec["command"]).is_absolute()
    assert spec["args"][:3] == ["-m", "insta_engine", "mcp"]


# -- 未指定 --target 时的自动检测 ---------------------------------------------

def _fake(monkeypatch, keys: dict[str, mi.McpTarget], detected: list[str]):
    monkeypatch.setattr(mi, "known_targets", lambda: keys)
    monkeypatch.setattr(mi, "detected_keys", lambda: detected)


def test_auto_picks_single_client(monkeypatch, tmp_path):
    """只命中一个就直接用它——大多数人不需要先知道自己用的客户端叫什么键。"""
    only = mi.McpTarget("only", "唯一客户端", tmp_path / "x" / "mcp.json")
    _fake(monkeypatch, {"only": only}, ["only"])
    got, err = mi.resolve_target(None)
    assert err is None and got is only


def test_auto_lists_choices_when_multiple(monkeypatch, tmp_path):
    """多个命中时必须给出**可复制**的命令，而不是交互提问（脚本/agent 答不了）。"""
    a = mi.McpTarget("a", "客户端 A", tmp_path / "a" / "mcp.json")
    b = mi.McpTarget("b", "客户端 B", tmp_path / "b" / "mcp.json")
    _fake(monkeypatch, {"a": a, "b": b}, ["a", "b"])
    got, err = mi.resolve_target(None)
    assert got is None
    assert "mat mcp install --target a" in err and "mat mcp install --target b" in err


def test_auto_explains_when_none_detected(monkeypatch, tmp_path):
    a = mi.McpTarget("a", "客户端 A", tmp_path / "a" / "mcp.json")
    _fake(monkeypatch, {"a": a}, [])
    got, err = mi.resolve_target(None)
    assert got is None
    assert "没检测到" in err and "--config" in err, "没检测到时必须给出兜底路径"


def test_explicit_unknown_target(monkeypatch, tmp_path):
    a = mi.McpTarget("a", "客户端 A", tmp_path / "a" / "mcp.json")
    _fake(monkeypatch, {"a": a}, ["a"])
    got, err = mi.resolve_target("nope")
    assert got is None and "未知" in err


def test_config_overrides_target(monkeypatch, tmp_path):
    _fake(monkeypatch, {}, [])
    got, err = mi.resolve_target(None, config=str(tmp_path / "custom.json"))
    assert err is None and got.key == "custom" and got.path == tmp_path / "custom.json"
