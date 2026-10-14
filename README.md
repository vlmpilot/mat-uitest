# MAT — Mobile AI Testing

![PyPI](https://img.shields.io/pypi/v/mat-uitest)

![Python](https://img.shields.io/pypi/pyversions/mat-uitest)

![CI](https://github.com/vlmpilot/mat-uitest/actions/workflows/ci.yml/badge.svg)

![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

AI 驱动的移动端 UI 自动化测试工具（Android 真机为主支持，iOS 模拟器预览）：自然语言生成用例，混合定位 + 失败自愈，每一步留下可追溯证据（截图 / 控件树 / logcat / 失败分类）。执行全程在本机，离线可用；用例是结构化 DSL——回归回放零 token，AI 只在生成与自愈时调用。

官网 [mobileaitest.com](https://mobileaitest.com)

## 安装与上手

按平台选一条（**Windows：pip 直装；macOS / Linux：pipx 隔离安装**）：

```bash
# Windows
py -m pip install --user mat-uitest

# macOS / Linux（没有 pipx 的话先 brew install pipx / sudo apt install pipx）
pipx install mat-uitest && pipx ensurepath
# 不想装 brew？用 uv（Linux 上尤其推荐：不需要 root，也不需要先装 Homebrew）
curl -LsSf https://astral.sh/uv/install.sh | sh   # 已有 uv 就跳过这行
uv tool install mat-uitest
uv tool update-shell                              # 把 ~/.local/bin 加进 PATH，重开终端后 mat 才能直接用

# 任何平台、不想装 pipx —— venv + pip
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install mat-uitest
```

```bash
mat init                              # 初始化工作区（含示例用例）
mat doctor                            # 环境自检：Python / adb / 设备 / AI 配置
mat run cases/example-settings.json   # 跑通第一条用例
```

要求：Python 3.11+、`adb`、一台开启 USB 调试的 Android 真机或模拟器（Android 真机与模拟器为主支持；iOS 见下）。命令找不到时：**macOS / Linux**（pipx 装）跑一次 `pipx ensurepath` 并重开终端；**Windows**（pip 直装）脚本目录 `%APPDATA%\Python\Python3x\Scripts` 可能不在 PATH，加入后重开终端。任何装法都可用等价写法 `python -m insta_engine`（Windows `py -m insta_engine`）。

> **iOS（模拟器预览）**：`mat run --platform ios`——需装 `mat-uitest[ios]` extra 且 WDA 跑在 `localhost:8100`。**真机未支持**，`index` / `explore` / `arrival` 在 iOS 上会明确拦截。

### 接入 AI

本地执行、控件层自愈与探索无需任何配置；`gen` / `describe` / `diagnose` 等生成类命令需在工作区 `.env` 配置二者其一。

**方式一：官方代理**

```bash
GEN_BASE_URL=https://mobileaitest.com
GEN_API_KEY=<你的 token>
GEN_PROMPT_MODE=server
```

**方式二：自带 Key（OpenAI 兼容端点）**

```bash
GEN_BASE_URL=<你的 OpenAI 兼容端点>
GEN_API_KEY=<你的 Key>
GEN_MODEL=<语言模型名>
GEN_VL_MODEL=<视觉模型名>
```

AI 命令只发送页面控件文本，不含截图。`GEN_MODEL` 是生成类命令（gen / describe / diagnose 等）的**主模型，必需**；`GEN_VL_MODEL` 仅用于视觉兜底（发送截图，默认关闭、需显式开启）——只配 VL 不能替代主模型。`mat export` 可一键导出全部本地数据。

## 常用命令

| 命令                                           | 用途                                                            |
| -------------------------------------------- | ------------------------------------------------------------- |
| `mat gen "<描述>" --package <pkg> --calibrate` | 自然语言生成用例（真机校准）                                                |
| `mat run cases/<file>.json`                  | 执行用例，产出证据链报告（HTML + JUnit XML）                                |
| `mat diagnose <run_dir>`                     | 失败归因：分类 + 证据 + 建议                                             |
| `mat explore <pkg> --minutes 2`              | 探索测试，发现问题附复现路径                                                |
| `mat index <pkg>`                            | 爬取页面控件清单（生成防幻觉底座 + 定位改名自愈）                                    |
| `mat console`                                | 本地控制台（`http://127.0.0.1:7420`，仅本机监听）——**工具所有功能和配置均可在本地控制台操作** |

## CI

```yaml
- uses: vlmpilot/mat-uitest@main   # self-hosted runner + Android 设备
  with:
    package: com.example.shop
    cases-dir: cases
```

任意 CI 系统：`mat run --junit-xml artifacts/mat-junit.xml`；完整 PR 门禁示例见 [examples/ci/mat-pr.yml](examples/ci/mat-pr.yml)。

## AI 编程助手接入

- **skill**（教助手何时跑用例、何时探索、怎么读结果）：`mat skill install` —— 把随包的 `mobileapp-testing` 装进 AI 助手的 skill 目录（默认 `~/.claude/skills/`；WorkBuddy 用 `--target workbuddy`，其他助手用 `--dir <目录>`）；`mat skill path` 打印包内位置供手动复制。装完开新会话才会加载
- **MCP**（让 Cursor / Codex / WorkBuddy 等直接驱动 `mat`）：装带 `[mcp]` extra 的包，再把 `mat mcp` 配进助手的 MCP 配置
  - Windows：`py -m pip install --user "mat-uitest[mcp]"`
  - macOS / Linux：`pipx install 'mat-uitest[mcp]'`
  - 配置片段（Cursor / CodeBuddy / Trae 通用）：`{"mcpServers": {"mat": {"type": "stdio", "command": "mat", "args": ["mcp"]}}}`；先手动跑一次 `mat mcp`，看到 `MAT MCP 就绪` 再填进 IDE

## 开发

```bash
git clone https://github.com/vlmpilot/mat-uitest.git && cd mat-uitest
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e './engine[dev]'
ruff check engine scripts && pytest engine/tests -q    # 全部走 FakeDriver 桩，无需真机
```

> 对外命令是 `mat`，Python 包名为历史命名 `insta_engine`，不影响使用。

文档：[使用手册](docs/13-产品使用手册.md) · [引擎设计](docs/22-引擎设计说明.md)

## 许可

AGPL-3.0（[LICENSE](LICENSE)）。贡献请先读 [CONTRIBUTING.md](CONTRIBUTING.md) 并签署 [CLA](CLA.md)。

## 商业授权（Commercial licensing）

默认许可是 AGPL-3.0。下面这些用法在 AGPL 之外，需要一份**书面商业授权**：把 MAT 集成进专有产品对外分发；或把它作为服务提供给第三方，而不愿公开对应源码。

反过来，**自己使用、内部修改、且只在组织内部运行**通常不需要商业授权——AGPL 的网络条款针对的是"向远程用户提供修改后的版本"。

不确定自己的场景属于哪一种，或需要商业授权：在本仓库提交 issue 说明使用场景，或关注公众号「**Mobile AI Test**」、加微信 **lazy_vizen**（备注「商业授权」）。
