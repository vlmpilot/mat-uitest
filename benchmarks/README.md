# benchmarks

任务集目录。从 spike-5 起步，按既定裁剪路线（8 App）逐步扩充。

**指标定义已公开**（见 [`METRICS.md`](METRICS.md)）——定义公开、真实跑测数据内部，两者不冲突；
标注规范（双人标注 + 仲裁）的**数据**不随源码发布。

## spike-5-tasks.json

- **t0**：冒烟任务，任意 Android 真机可跑（启动系统设置）
- **t1~t4**：依赖 `examples/demo-app` 靶场（构建规格见该目录 README，Android Studio 约半天）
- 通过标准：**步骤成功率 ≥80%**，由 `scripts/spike_check.py` 自动判定
- 每个 task = `nl`（自然语言描述）+ `case`（gold DSL，与 `insta_engine.dsl` 同 schema）

## public-subset.json（可对外复现的最小评测子集）

- **靶场**：`examples/demo-app`（源码自带白屏缺陷，`BlankActivity` 注释写明**勿修**）。
  从当前源码构建的 debug APK 就是"带缺陷版"；参考构建指纹见 JSON 的 `apps` 段
  （仅用于核对是否同一棵树，以你本地构建为准）。
- **13 条任务**：10 条常规流程（`expect: pass`）+ 3 条公告页缺陷（`expect: fail`）。
  `expect: fail` 的任务**通过 = 假绿**；若靠自愈救回，计**误愈**。
- **复现**：

  ```bash
  cd examples/demo-app && gradle :app:assembleDebug
  adb install -r app/build/outputs/apk/debug/app-debug.apk

  cd ../..
  mat eval run benchmarks/tasks/public-subset.json --repeat 3 --serial <你的设备>
  ```

- **看什么**：`overall.median`（通过率）、`overall.false_green.rate`（缺陷检出是否可信）、
  `overall.false_heal`（自愈有没有放过缺陷）。口径见 `METRICS.md`。
- 3 条缺陷任务**预期失败**是正常的：整体 `pass_threshold` 因此为 0.7，不是 1.0。

## 约定

- spike 阶段跑 **gold DSL**，度量的是引擎稳定性；NL 生成质量（一次可用率）单独度量，不混在这套任务里
- 扩充任务保持 schema 一致；gold 用例变更走 PR 并在描述里注明原因
- 对抗集（故意改版/埋 bug）在 V1.5 引入，demo-app 的故意 bug 清单见 examples/demo-app/README.md

## page_identity/

**页面身份的离线回归语料**，由 `scripts/build_page_identity_corpus.py` 从真实设备录制产物生成，
供 `engine/tests/test_page_identity_corpus.py` 使用。用于在**不插设备、不调模型**的前提下测
`page_key` / `_identities` / `_candidate` 在真实数据上的表现（这类判据读代码测不出来）。

⚠️ **数据本身不进公开仓库**：语料是真实 App 的 UI 结构，属"真实跑测数据"，按项目
「真实跑测数据一律不对外」的边界排除。本仓库里该目录不存在，相关测试自动 skip。

## results/

**评测记录**（内部资料，不随源码导出）。每轮跑测落两份东西：

- `ledger.jsonl`：机器可读台账，每臂一行（日期 / 基准 / 臂名 / 平台 / 通过数 / 耗时 /
  引擎版本 / 产物路径 / 失败清单）。由 `scripts/bench_ledger.py` **从 `run.json` 逐条统计**生成，
  不接受手填数字；
- `<日期>-<主题>.md`：当轮的可读记录（矩阵、失败步、复现命令、限制）。

为什么要有它：`artifacts/` 被 `.gitignore` 忽略，换机/清目录就没了；而只把汇总数字写进文档，
下次改 prompt 或换模型想对比时只能凭记忆。台账只存摘要，所以小、能入库、能 diff。

```bash
python scripts/bench_ledger.py list      # 看所有臂
python scripts/bench_ledger.py verify    # 校验台账与磁盘是否还一致（产物被清理时保留记录）
```

### 现有记录

| 文件 | 内容 | 关键数字 |
|---|---|---|
| `2026-10-20-defect-probes.md` | 植入缺陷检出率（B1），Android + iOS 靶场 | 7/7、5/5，误报 0 |
| `2026-10-21-first-run.md` | 一次通过率三轮迭代 | 14% → 24% → 33% |
| `2026-10-22-false-green.md` | **假绿率（A1）+ 强断言覆盖率（B2b）** | 假绿 **3/7**（95% CI 16%–75%）；强断言 **0.01** |

> 三个数字的关系：「一次通过率」说"生成的东西有多少能一次跑通"，「假绿率」说"**跑通的那些能不能信**"，
> 「强断言覆盖率」说"为什么不能信"。**只看一次通过率会朝错误方向优化**——把用例写松一点，这个数立刻变好。
