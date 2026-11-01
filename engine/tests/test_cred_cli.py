"""凭据 CLI 的非交互入口（`mat cred set --stdin`）。

为什么单独立一组：`mat cred set` 原来是纯交互（`getpass`），agent / CI 只要走到
「登录用例要凭据」就卡死——而登录是最高频的测试场景。`--stdin` 必须满足三条：
值原样保存（含空格）、不进 shell history（所以不提供 `--value`）、空输入要明确报错。

用子进程测：这才是 agent 实际调用的形态；直调 `cmd_cred` 会绕过 argparse 与 stdio。
"""

from __future__ import annotations

import json
import subprocess
import sys

from insta_engine.credentials import CredentialStore


def _run(args: list[str], home, stdin: str = "") -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "INSTA_HOME": str(home), "HOME": str(home),
           "LANG": "C.UTF-8"}
    return subprocess.run([sys.executable, "-m", "insta_engine", *args], input=stdin,
                          capture_output=True, text=True, timeout=120, env=env)


def test_cred_set_from_stdin_keeps_value_verbatim(tmp_path):
    """值里的空格不能被吞——密码/令牌里带空格是常见的。"""
    home, ws = tmp_path / "home", tmp_path / "ws"
    ws.mkdir()

    r = _run(["cred", "set", "测试号", "--stdin"], home, stdin="p@ss word 两个空格\n")

    assert r.returncode == 0, r.stderr
    stored = CredentialStore(home / "credentials.json")._read()
    assert stored["测试号"] == "p@ss word 两个空格", "只该去掉行尾换行，别 strip 其它空白"


def test_cred_set_empty_stdin_fails_with_hint(tmp_path):
    """空输入不能静默存一个空密码——那会让登录用例失败得莫名其妙。"""
    r = _run(["cred", "set", "u", "--stdin"], tmp_path / "home", stdin="")

    assert r.returncode == 1
    assert "--stdin" in r.stderr and "stdin" in r.stderr


def test_cred_list_shows_names_only(tmp_path):
    """列表永不回值——凭据出现在终端/日志里就等于泄漏。"""
    home = tmp_path / "home"
    assert _run(["cred", "set", "u1", "--stdin"], home, stdin="topsecret\n").returncode == 0

    r = _run(["cred", "list"], home)

    assert r.returncode == 0
    assert "u1" in r.stdout
    assert "topsecret" not in r.stdout


def test_cred_stdin_value_never_lands_in_shell_history(tmp_path):
    """`--value` 这类参数刻意不存在——值会进 history。检查帮助面里没有它的入口。"""
    home = tmp_path / "home"
    r = _run(["cred", "set", "--help"], home)

    assert r.returncode == 0
    assert "--stdin" in r.stdout
    assert "--value" not in r.stdout


def test_credential_file_is_0600(tmp_path):
    """凭据落盘权限：同机其它用户不该能读。"""
    home = tmp_path / "home"
    _run(["cred", "set", "u", "--stdin"], home, stdin="x\n")

    mode = (home / "credentials.json").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_cred_file_is_plain_json(tmp_path):
    """存储格式保持简单（v0）：名称 → 值，便于人工排查**不带出**到日志。"""
    home = tmp_path / "home"
    _run(["cred", "set", "u", "--stdin"], home, stdin="v\n")

    data = json.loads((home / "credentials.json").read_text(encoding="utf-8"))
    assert data == {"u": "v"}
