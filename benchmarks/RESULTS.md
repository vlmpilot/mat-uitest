# 公开子集 · 评测结果

> 数字由脚本**从 `run.json` 逐条读取**后生成，未手工填写。产物目录（截图与控件树）
> 不进版本库；复现命令见 [`README.md`](README.md)。

## 2026-11-23 · 首份结果

### 环境指纹

| 项 | 值 |
|---|---|
| 引擎 | 0.8.0 + 未发布修复（输入回读改双向包含） |
| 靶场 | `com.insta.demo`，`app-debug.apk` sha256 `9ffeefbf780c…`（由 `examples/demo-app` 当前源码构建） |
| 设备 | ELE-AL00 · Android 10 · `8KE5T19819001756` |
| 任务 / 遍数 | 17 条 × 3 遍 = 51 次运行 |
| 步骤 | 444 / 474 通过（93.7%） |

### 任务三态（k=3）

| 状态 | 数量 | 任务 |
|---|---|---|
| 3/3 稳定通过 | 10 | `demo-a01-login-elements`, `demo-a02-list-elements`, `demo-e01-wrong-password`, `demo-e02-empty-password`, `demo-e03-search-case-sensitive`, `demo-f01-login-ok`, `demo-f02-search-bluetooth`, `demo-f03-search-headphone`, `demo-f04-search-no-hit`, `demo-f05-search-then-detail` |
| 0/3 稳定失败 | 7 | `defect-crash-export`, `defect-empty-search-undefined`, `defect-list-count-wrong`, `defect-login-btn-should-disable`, `defect-longpress-no-menu`, `defect-no-empty-hint`, `defect-search-truncated` |
| 不稳定 | 0 | （无） |

### 缺陷检出（7 条 `expect: fail` 任务）

| 指标 | 值 |
|---|---|
| 假绿率 | **0/21 = 0.0%**（95% Wilson 区间 0.0%–15.5%） |
| 缺陷任务稳定失败 | **7/7** |
| 失败步是否只在断言步 | **是**（定位/环境失败会把"没测到"伪装成"检出"） |

7 条缺陷的失败信息（每条 3 遍一致，只有断言步失败）：

```
defect-empty-search-undefined     控件不应存在: undefined
defect-login-btn-should-disable   控件状态应为 disabled: com.insta.demo:id/login_btn
defect-longpress-no-menu          控件应存在: 删除
defect-list-count-wrong           控件应存在: 共 2 项
defect-search-truncated           期望文本 'abcdefghijklmn'，实际 'abcdefghij'
defect-no-empty-hint              控件应存在: 没有找到相关结果
defect-crash-export               控件应存在: com.insta.demo:id/export_result
```

### 怎么读这组数字（限制）

- **单设备、单 App、单日**：17 条任务都跑在同一个靶场（源码自带缺陷）。它说明"引擎在
  这份靶场上的行为可复现"，**不能外推**到第三方 App 的成功率。
- **7 条缺陷任务预期失败**：整体"通过率"没有意义，该看的是假绿率与任务三态。
- **假绿率的区间**：0/21 的点估计是 0，但 95% 上限约 15%——样本还小，别读成"绝不假绿"。
- **缺陷任务的失败原因已逐条核对**：全部失败在断言步，没有一条是定位失败
  （否则就是"没测到"冒充"检出"）。
- 崩溃 3 次来自 `defect-crash-export`——这条缺陷本身就是"点导出即崩溃"，是预期行为。

### 复现

见 [`README.md`](README.md) 的 public-subset 一节。
