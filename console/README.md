# console/

**本地控制台的 V2（Ant Design 版）** —— 已实现并**随包分发**：`mat console` 默认打开的就是这一版。

- **技术栈**：React 18 + TypeScript + Ant Design 5 + Vite。纯前端静态资源，不额外起后端进程
  —— 它消费的是同一个本机 HTTP 服务（`engine/insta_engine/web_server.py`）。
- **视图（7 个）**：概览 / 用例 / 生成 & 导入 / 执行 / 报告 / 页面复核 / 设置
- **关键组件**：
  - 设备卡片——**实时投屏**（scrcpy）+ 按键注入
  - 任务监控——步骤事件流 + **真中止**（不是只能等它跑完）
  - 命令面板、环境自检卡片、页面复核视图（页面标题的人工确认与回写）

## 构建与分发

```bash
bash scripts/build_console.sh     # npm install → vite build → 同步到 engine/insta_engine/console_dist/
```

- 产物 `engine/insta_engine/console_dist/` 是**构建产物**（在 `.gitignore` 里，不追踪）。
  wheel 打包**依赖它存在** ⇒ 发布前必须先跑构建脚本（发版脚本已把这个顺序编进去）。
- 开发态：`npm --prefix console run dev` 起 Vite dev server，配合 `mat console --no-open` 起的后端。

## 与 `engine/insta_engine/web/` 的关系

`web_server.py` 按「**存在即优先**」选前端：

| 目录 | 形态 | 说明 |
|---|---|---|
| `engine/insta_engine/console_dist/` | 本目录的构建产物 | 存在即优先服务 |
| `engine/insta_engine/web/` | 轻量单页（原生 JS，无构建） | 回退；控制台的早期形态，接口覆盖是 AntD 版的子集 —— 实时投屏 / 真中止 / 按键注入 / 页面复核只在 AntD 版有 |

`GET /api/state` 的 `console` 字段会告诉你当前跑的是哪一版：`pro` = 本版，`legacy` = 轻量回退版。
