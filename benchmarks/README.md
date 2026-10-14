# benchmarks

任务集目录。从 spike-5 起步，按既定裁剪路线（8 App）逐步扩充；标注规范（双人标注+仲裁）与指标定义属内部评测资料，不随源码公开。

## spike-5-tasks.json

- **t0**：冒烟任务，任意 Android 真机可跑（启动系统设置）
- **t1~t4**：依赖 `examples/demo-app` 靶场（构建规格见该目录 README，Android Studio 约半天）
- 通过标准：**步骤成功率 ≥80%**，由 `scripts/spike_check.py` 自动判定
- 每个 task = `nl`（自然语言描述）+ `case`（gold DSL，与 `insta_engine.dsl` 同 schema）

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
