# grounding 数据集 · demo-app

真值框来自 `uiautomator` 的控件树 `bounds`（**不是人工标注**）：靶场是原生控件，
系统给的位置就是权威值，别人可以重新采集核对。

| 文件 | 内容 |
|---|---|
| `shots.json` | 目标清单：`intent` / `desc` / `bbox` / `bucket` / `kind` / 控件文本 |
| `shots/*.png` | 原分辨率截图（真值换算依赖原图，不能拿缩略图） |
| `shots/*.xml` | 采集时的控件树原文（复核真值用） |
| `result-*.json` | 某次评测的逐目标结果（含坐标、命中、耗时） |

## 重新采集 / 评测

```bash
python scripts/grounding_capture.py --serial <设备> --out benchmarks/grounding/demo-app
python scripts/grounding_eval.py --data benchmarks/grounding/demo-app/shots.json --repeat 3
```

## 覆盖范围与限制

- **只覆盖原生控件**：真值来自控件树，自绘界面（canvas/WebView/Flutter）没有 bounds，
  这类目标不在本数据集里。
- `desc` 刻意**不含位置线索**（不写"右上角"），否则测的是"照指令找"而不是"看懂界面"。
- 目标是"可点的一整块"，不是像素级中心——大面积容器（列表、输入框）的"中心"本来
  就没有唯一解，看命中率时别按单点较真，按框内/框外看。
