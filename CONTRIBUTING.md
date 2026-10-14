# 贡献指南

感谢关注MAT。本项目处于 spike 阶段，欢迎 issue 讨论；PR 请先读完本指南。

## 开发环境

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e "./engine[dev]"
```

## 代码规范

- 格式与 lint：`ruff check engine scripts`（行宽 100，配置见 `engine/pyproject.toml`）
- 测试：`pytest engine/tests`
- 提交信息：`<scope>: <一句话>`，如 `engine: 修复 input 步骤后 post-verify 误报`

## 踩坑即沉淀（我们的工程习惯）

踩到非平凡的问题时，**当场记一条**，并写清三件事：**现象**（带报错原文）、**根因**、**下次怎么自动拦住它**。

- 判断标准：排查超过 10 分钟 / 会导致错误结论 / 涉及安全合规 / **重复出现第二次**。
- **「下次怎么自动拦住」是硬要求**——优先加测试或检查；加不上就说明为什么。
  文档提醒拦不住手滑，能自动化的不要只写文档。
- 报告问题 / 提 PR 时带上这条，比"修好了"有用得多。

## CLA（必须）

首次提交 PR 时需签署 [CLA.md](CLA.md)（CLA Assistant 机器人会自动校验）。这是保证项目未来能维持 open-core 双许可的前提，感谢理解。

## 开放核心边界（写码前必读）

- 本仓库只放**开源核心**：DSL、引擎壳、CLI、配置台、prompt 公开模板；
- **调优后的私有 prompt**（`prompts/*.prompt` 与私有 prompt 目录）、评测回路参数、
  云端服务代码**不要**提交进来（已 gitignore）；
- 不确定某个东西该不该入库时，先开 issue 讨论。

## 行为准则

保持友善与专业；测试是工程的良心，我们对自己也用同一标准。

## 发布节奏（维护者）

- **PyPI 发版 = 攒批发版**：改动先在本地完整验证（pytest 全绿 + 真机冒烟 + 发起人确认），
  攒成一批再发一个版本号；**禁止每改一次就发一版**——PyPI 是发布渠道，不是备份机制。
- **发版构建**：**`bash scripts/build_release.sh`**——发布物只从**对外发布用的源码树**构建
  （不要用带内部资料的开发树）。脚本自动完成：前端构建 → 源码同步 → `console_dist` 补齐
  → wheel + twine check（三件套内嵌于脚本）。⚠️ 该脚本属维护者内部发布工具，
  **不在公开仓库内**（公开仓的验证路径见上：`pip install -e './engine[dev]'` + ruff + pytest）。
- 版本号：向后兼容修复升 patch（0.4.x）；新增用户可见能力升 minor（0.5.0）。
