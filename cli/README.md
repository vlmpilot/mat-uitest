# cli/

**当前发布形态是 Python 包 [`mat-uitest`](https://pypi.org/project/mat-uitest/)**：**Windows**：`py -m pip install --user mat-uitest`；**macOS / Linux**：`pipx install mat-uitest && pipx ensurepath`，得到 `mat` 命令（不想装 brew 就改用 uv：`https://astral.sh/uv/install.sh` 装 uv 后 `uv tool install mat-uitest && uv tool update-shell`）。实现在 [`engine/insta_engine/`](../engine/insta_engine/)（入口 `__main__.py`，全部子命令见根 README 与[使用手册](../docs/13-产品使用手册.md)）。

本目录预留给规划中的 **Go 单二进制**形态（零依赖安装、内嵌 adb 与驱动管理、自动更新），**尚未启动**：pipx/uv 安装已覆盖当前用户（开发者与测试工程师），Go 形态解决的是"非 Python 用户的安装体验"——等真实反馈表明这是主要使用门槛时再投入。
