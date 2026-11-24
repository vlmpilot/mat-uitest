# 公开评测结果

> 数字由脚本**从 `run.json` 逐条读取**后生成，未手工填写。产物目录（截图与控件树）
> 不进版本库；复现命令见 [`README.md`](README.md)。

## 2026-11-24 · grounding 命中率（9 目标 × 3 遍）

> 真值来自控件树 `bounds`（不是人工标注），采集脚本 `scripts/grounding_capture.py`；
> 判定口径与容差在 `scripts/grounding_eval.py` 里写死（默认容差 0）。

| 项 | 值 |
|---|---|
| 数据 | `benchmarks/grounding/demo-app/shots.json`（9 个目标，3 页） |
| 模型 | `qwen3-vl-plus` |
| 靶场 | `com.insta.demo`，apk sha256 `9ffeefbf780c69b4…`，1080×2340 |
| 命中率 | **26/27 = 96.3%**（95% Wilson 81.7%–99.3%） |
| 模型判「目标不在图上」 | 0 次 |
| 调用失败（不进分母） | 0 次 |

**分桶**：`middle` 11/12、`top` 12/12、`bottom` 3/3　**分类型**：`button` 9/9、`input` 8/9、`text` 6/6、`container` 3/3

### 唯一未命中

- `login/password` 返回 `[1, 867]`，真值框 `[72, 781, 1008, 949]`（x 越界 71px，y 在框内）

这类"y 轴准、x 轴贴边"的退化在端到端评测里表现为"偶尔点不中"，看不出原因；
grounding 指标能直接定位到是坐标问题而不是定位失败。

### 坐标抖动（同一图、同一目标、3 次调用）

| 目标 | 最大偏差 |
|---|---|
| `login/password` | 538px |
| `list/list_view` | 24px |
| `detail/detail_title` | 5px |
| `login/announce_btn` | 2px |

5/9 个目标 3 次返回**完全相同**的坐标。抖动最大的是密码框（538px，就是那次贴着
左边缘的退化），其余在 2–24px（24px 的是列表容器——大面积目标的"中心"本来就没有
唯一解，这类目标不该按框中心判定对错）。

### 限制

- **n = 27 偏小**：96% 的 95% 区间是 82%–99%，
  只有 9 个目标，不能外推到第三方 App。
- **只覆盖原生控件**：真值来自控件树，自绘界面（canvas/WebView/Flutter）没有 bounds，
  本数据集不覆盖——这类目标的定位准确率需要另一套标注方式。
- **单设备、单模型、单日**。

复现命令与数据集说明见 [`grounding/demo-app/README.md`](grounding/demo-app/README.md)。

## 2026-11-23 · 公开子集（17 条 × 3 遍）

### 环境指纹

| 项 | 值 |
|---|---|
| 引擎 | 0.8.1（含输入回读双向包含修复） |
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
