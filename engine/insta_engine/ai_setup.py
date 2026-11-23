"""AI 配置引导（单一话术源）：缺什么、怎么配、怎么复检。

`GenClient.from_env`（CLI / 引擎全部 AI 命令的入口）、MCP 工具提交前预检
（`mcp_server.mat_generate_case`）、skill 文档三处都要同一份「AI 未配置怎么办」
的话术——散写必然漂移，集中在这里。零依赖：只读 os.environ 与 promptstore 的
模式判定，三方都能安全 import，不引循环。

口径与 `doctor.ENV_TEMPLATE`、README「AI 接入」两段保持一致：
方式一 BYOK（推荐，数据不经 MAT 服务器）、方式二官方代理（零模型配置）。
**改一处必须同步另外两处。** 本文件会随引擎进公开仓导出物：注释里不写
经营性字样（导出护栏按词 fail-closed，描述红线的话也会被拦），token 获取
渠道只写公众号名（与 doctor/README 既有口径一致）。
"""

from __future__ import annotations

import os

SERVER_BASE_URL = "https://mobileaitest.com"

BYOK_SNIPPET = (
    "# 方式一（推荐）：自带 Key——prompt 本机拼装，控件文本与截图只发往你自己的端点\n"
    "GEN_BASE_URL=<你的 OpenAI 兼容端点>\n"
    "GEN_API_KEY=<你的 Key>\n"
    "GEN_MODEL=<语言模型名>"
)

SERVER_SNIPPET = (
    "# 方式二：官方代理（零模型配置）——由服务端组装 prompt，模型由服务端决定\n"
    f"GEN_BASE_URL={SERVER_BASE_URL}\n"
    "GEN_API_KEY=<平台代理token>   # 关注公众号「Mobile AI Test」获取代理token\n"
    "GEN_PROMPT_MODE=server"
)


def missing_env_vars() -> list[str]:
    """AI 生成能力当前缺哪些环境变量（返回空列表 = 配置齐全）。

    与 `GenClient.from_env` 的检查同源：server 模式（GEN_PROMPT_MODE=server）
    模型由服务端决定，本地不需要 GEN_MODEL；BYOK（local）模式三件套缺一不可。
    纯读环境变量，无副作用、无网络——MCP 工具可以在提交任务前用它做预检。
    """
    from . import promptstore
    from .offline import is_private_endpoint

    base = os.environ.get("GEN_BASE_URL", "")
    # 私有端点（本机 / MAT_PRIVATE_HOSTS）常是内网网关，可能不校验 Key：不强制要求。
    # 公网端点（官方代理由服务端校验、BYOK 由厂商校验）仍然要 Key。
    need = ("GEN_BASE_URL",) if is_private_endpoint(base) else ("GEN_BASE_URL", "GEN_API_KEY")
    missing = [name for name in need if not os.environ.get(name)]
    if not promptstore.is_server() and not os.environ.get("GEN_MODEL"):
        missing.append("GEN_MODEL")
    return missing


def ai_setup_hint(missing: list[str], env_path: str | None = None) -> str:
    """给 agent 与人共用的可执行引导：缺什么 + 写到哪 + 可粘贴片段 + 复检方式。

    CLI 里作为 GenError 消息原样走 stderr，MCP 工具放进 error 字段——第一行
    保留「缺少环境变量:」前缀，与既有测试断言（test_web_api 的诊断 job）兼容。
    `env_path` 只由**知道工作区根的调用方**传入（CLI=cwd/.env，MCP=workspace/.env）；
    `from_env` 这类通用入口不要猜路径（控制台进程 cwd 是引擎安装目录，猜必错），
    保持 None 走「工作区 .env」的通用表述。控制台「打开配置」按钮在文件不存在时
    会以 `doctor.ENV_TEMPLATE` 自动铺模板，agent 自己写文件同理。
    """
    joined = ", ".join(missing)
    if env_path:
        target_line = (f"把下面任意一种（二选一）写入 `{env_path}`"
                       "（文件可不存在，写入即创建），写完跑 `mat doctor` 复检：")
    else:
        target_line = "在工作区 .env 写入下面任意一种（二选一），写完跑 `mat doctor` 复检："
    lines = [
        f"缺少环境变量: {joined}",
        "AI 生成（gen / describe / diagnose）未配置；本地执行、回放、探索不依赖 AI，不受影响。",
        target_line,
        "",
        BYOK_SNIPPET,
        "",
        SERVER_SNIPPET,
    ]
    return "\n".join(lines)
