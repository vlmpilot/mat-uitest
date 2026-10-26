"""按应用的测试说明（白/黑名单、账号、风控注意等负责人补充）。

存 `<工作区>/app_notes/<包名>.md`：一应用一文件、纯 Markdown 自由文本、文件即接口
（可直接用编辑器改，控制台只是另一个编辑入口）。生成用例（gen）与润色（polish）
发起时后端自动读取，经 `app_info.user_notes` 随模板 `{app_info}` 占位符进 prompt
——local / server 两种模式同一条注入路径，不改任何 prompt 模板（存量工作区
无需重播种）。
"""

from __future__ import annotations

from pathlib import Path

from .pkgname import safe_package_name

#: 注入 prompt 时的自解释前缀：模板不动，靠字段内容说明自己是什么、优先级多高。
PREFIX = "以下是测试负责人对本应用的补充说明（黑/白名单、禁止操作、账号等），优先级高于以上通用规范："

#: 长度上限：说明是「约束」不是「文档」；超限静默截断（写入不拦，读取注入时截）。
MAX_CHARS = 4000


def note_path(root: Path, package: str) -> Path:
    pkg = safe_package_name(package)
    return Path(root) / "app_notes" / f"{pkg}.md"


def read(root: Path, package: str) -> str:
    """读该应用的说明；无文件/读失败返回空串（缺说明不是错误，不挡生成）。"""
    try:
        text = note_path(root, package).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""
    return text


def write(root: Path, package: str, text: str) -> None:
    """写入说明；空文本删文件（清空即恢复默认，不留零字节孤儿）。"""
    path = note_path(root, package)
    content = (text or "").strip()
    if not content:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


def for_prompt(root: Path, package: str) -> str | None:
    """生成给 prompt 的注入文本；无说明返回 None（调用方按「无」处理，不塞空串）。"""
    text = read(root, package)[:MAX_CHARS]
    if not text:
        return None
    return f"{PREFIX}\n{text}"
