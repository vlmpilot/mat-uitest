# MAT Engine

AI 移动端 App UI 自动化测试工具——执行引擎与 CLI（命令 `mat`）。

- 自然语言一句话生成用例，真机 dry-run 校准
- 混合驱动定位链：控件层 → 图像模板 → 视觉大模型兜底，失败自动自愈
- 每步强制截图证据，失败五分类归因（崩溃 / 断言失败 / 定位失败 / 环境问题 / 待归因）
- 探索测试：规则驱动自主遍历，页面层级（L0-Ln）与页面图谱沉淀，发现附复现路径
- 本地执行免费不限量，数据不出本机

```bash
# Windows
py -m pip install --user mat-uitest
# macOS / Linux（推荐：uv 自动解决 Python 3.11+，不需要预装；已有 uv 跳过第一行）
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install mat-uitest && uv tool update-shell   # 最后一步把 ~/.local/bin 加进 PATH，重开终端生效
# 已装 pipx（等价备选）：pipx install mat-uitest && pipx ensurepath

mat doctor   # 环境自检（首次启动控制台会自动建好工作区与示例用例）
```

文档：https://mobileaitest.com/manual.html
