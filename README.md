# MAT — Mobile AI Testing

![PyPI](https://img.shields.io/pypi/v/mat-uitest)

![Python](https://img.shields.io/pypi/pyversions/mat-uitest)

![CI](https://github.com/vlmpilot/mat-uitest/actions/workflows/ci.yml/badge.svg)

![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

AI 驱动的移动端 UI 自动化测试工具（Android 真机为主支持，iOS 模拟器 + 真机 beta）：自然语言生成用例，混合定位 + 失败自愈，每一步留下可追溯证据（截图 / 控件树 / logcat / 失败分类）。执行全程在本机，离线可用；用例是结构化 DSL——回归回放零 token，AI 只在生成与自愈时调用。

官网 [mobileaitest.com](https://mobileaitest.com)

## 安装与上手

按平台选一条（**macOS / Linux：uv 安装，推荐；Windows：pip 直装**）：

```bash
# macOS / Linux（推荐：uv 自动解决 Python 3.11+，不需要预装）
curl -LsSf https://astral.sh/uv/install.sh | sh   # 已有 uv 就跳过这行
uv tool install mat-uitest
uv tool update-shell                              # 把 ~/.local/bin 加进 PATH，重开终端后 mat 才能直接用

# Windows（推荐：py 启动器随 Python 安装包附带）
py -m pip install --user mat-uitest

# 已装 pipx（等价备选；内网下载 uv 托管 Python 受限时用这条）
pipx install mat-uitest && pipx ensurepath        # 没有 pipx：brew install pipx（macOS）/ sudo apt install pipx（Ubuntu·Debian）

# 任何平台、不想装 uv / pipx —— venv + pip
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install mat-uitest
```

```bash
mat init                              # 初始化工作区（含示例用例）
mat doctor                            # 环境自检：Python / adb / 设备 / AI 配置
mat run cases/example-settings.json   # 跑通第一条用例
```

要求：Python 3.11+（uv 安装会自动带上）、`adb`、一台开启 USB 调试的 Android 真机或模拟器（Android 真机与模拟器为主支持；iOS 见下）。命令找不到时：uv 装的跑一次 `uv tool update-shell`、pipx 装的跑一次 `pipx ensurepath`，然后重开终端；**Windows**（pip 直装）脚本目录 `%APPDATA%\Python\Python3x\Scripts` 可能不在 PATH，加入后重开终端。任何装法都可用等价写法 `python -m insta_engine`（Windows `py -m insta_engine`）。

> **iOS**：装 `mat-uitest[ios]` extra。模拟器：`mat run --platform ios`（WDA 跑在 `localhost:8100`）。
> 真机（beta）：USB 连接后先 `mat ios-setup` 逐项体检（首次需一次性准备：Xcode 登录账号、
> 设备信任证书、在 Xcode 里 ⌘U 一次），然后 `mat run --platform ios --serial <udid>`。
> 真机限制：仅 USB、须保持解锁且自动锁定设为「永不」、免费证书 7 天过期、不支持录屏；
> `index` / `explore` / `arrival` 在 iOS 上明确拦截。

### 接入 AI

本地执行、控件层自愈与探索无需任何配置；`gen` / `describe` / `diagnose` 等生成类命令需在工作区 `.env` 配置二者其一。

**方式一：自带 API Key（推荐——数据不经我们）**

```bash
GEN_BASE_URL=<你的 OpenAI 兼容端点>
GEN_API_KEY=<你的 Key>
GEN_MODEL=<语言模型名>
GEN_VL_MODEL=<视觉模型名>   # 可选：视觉兜底，不配则关闭
GEN_VL_BASE_URL=<视觉端点>  # 可选：兜底单独走一个端点（如本机模型）
MAT_VL_MAX_EDGE=1280        # 可选：送模型前把长边压到这个值（默认不缩）
```

prompt 在你本机拼装（`GEN_PROMPT_MODE` 默认 `local`），请求直连你的端点：控件文本与截图**不经过 MAT 服务器**。模型可自选（DashScope / 智谱 / DeepSeek / OpenAI / 企业内网网关）。

**方式二：官方代理（零配置试用）**

```bash
GEN_BASE_URL=https://mobileaitest.com
GEN_API_KEY=<你的 token>
GEN_PROMPT_MODE=server
```

装完不配 API Key 也能先看效果（体验模式 60 次/月）。此模式下由服务端组装 prompt 并转发给模型厂商：**控件文本会经我们转出，开启视觉兜底时截图也会转出**；我们不做内容留存、不用于训练。

> 数据流向是两条路最大的区别：要「内容不出本机」，用方式一；只有方式二会把请求交给我们中转。代理的 prompt 档与本地档同源（gen v1.2），差异只在是否需要自己配 API Key。

`GEN_MODEL` 是生成类命令（gen / describe / diagnose 等）的**主模型，必需**；`GEN_VL_MODEL` 仅用于视觉兜底（发送截图，默认关闭、需显式开启）——只配 VL 不能替代主模型。`GEN_VL_BASE_URL` 让兜底单独走另一个端点，最常见用法是**生成留在云端、兜底换成本机模型**（截图不出本机）；端点填 `127.0.0.1` 时若系统开了 `http_proxy`，记得 `NO_PROXY=127.0.0.1,localhost`，否则请求会被代理转走（表现是 502）。`MAT_VL_MAX_EDGE` 只缩**发给模型的那份**截图（坐标仍按原图换算，精度不受影响）——本机小模型嫌慢时设 1280，单次可从 7~8 秒降到 3~4 秒；不设则原样发送。**端点在本机时请求自动走 OpenAI 兼容协议**（`/v1/chat/completions`，与 `GEN_PROMPT_MODE` 无关——本机模型服务普遍只提供这一种接口，走官方代理的私有协议会 404），这类调用也**不占官方视觉额度**（平台没出 token 钱）。`mat export` 可一键导出全部本地数据。

> `MAT_OFFLINE=1`（离线模式）只拒绝**出网**请求；端点在本机（`127.0.0.1` / `localhost`）时照常放行——「私有化部署 + 本机小模型」因此可以同时成立。

## 使用方式与获取

本地执行、回放、自愈、证据报告、多人协作**全程免费、不限量**；只有官方 AI 代理这条通道计数。

⚠️ **使用方式**（体验 / 标准 / 团队）与 **AI 接入**（官方代理 / 自带 API Key）是两个独立维度：三种方式都可以自带 API Key，自带 API Key 时本站不设上限；差别只在官方代理的月度额度。

| 方式 | 官方代理额度 | 自带 API Key | 适合 |
| --- | --- | --- | --- |
| **体验模式**（装完即用） | 生成 60 / 视觉 20 次每月 | 不设上限 | 第一次装、先看效果 |
| **标准模式**（激活后） | 生成 300 / 视觉 100 次每月 · 公众号 / 邮件人工回复（工作日 48 小时内） | 不设上限 | 每天都在跑、要更高额度 |
| **团队模式**（`mat console --share`） | 同标准模式 | 不设上限 | 2–5 人共用一套控制台；成员名额只在这种模式下出现 |

标准模式的激活码：关注公众号「Mobile AI Test」参加用户调研获赠，或留言申请。完整对照见 [使用方式](https://mobileaitest.com/plans.html)。

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

- **skill**（教助手何时跑用例、何时探索、怎么读结果）：`mat skill install` —— 把随包的 `mobileapp-testing` 装进 AI 助手的 skill 目录（默认 `~/.claude/skills/`；WorkBuddy 用 `--target workbuddy`，其他助手用 `--dir <目录>`）；`mat skill path` 打印包内位置供手动复制。装完开新会话才会加载。助手不能执行终端命令时，从 [mat-skill](https://github.com/vlmpilot/mat-skill) 取 `mobileapp-testing/` 目录直接导入（手动拷贝的副本不会自动更新）
- **MCP**（让 Cursor / Codex / WorkBuddy 等直接驱动 `mat`）：装带 `[mcp]` extra 的包，再把 `mat mcp` 配进助手的 MCP 配置
  - Windows：`py -m pip install --user "mat-uitest[mcp]"`
  - macOS / Linux：`uv tool install 'mat-uitest[mcp]'`（已装过普通版再跑这条即可带上 MCP 依赖；pipx 用户等价写法 `pipx install 'mat-uitest[mcp]'`）
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

