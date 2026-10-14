"""配置台 API 的按域实现（Phase 0 分层）。

`web_server.py` 只保留 HTTP 层（Handler/路由表/静态文件/serve），全部 `api_*`
处理函数按域拆到本包；`web_server` 逐名重导出以保持既有导入面
（tests / mcp_server 的 `ws.api_*` 不变）。

域划分：
- runtime   共享基建：任务队列/驱动缓存/设备锁/源码新鲜度/safe_join
- workspace 工作区清单读取（case_meta/list_runs/imports/explores/indexes）
- state     /api/state /api/doctor /api/activate
- cases     /api/case GET/POST + /api/import/preview|convert
- jobs      /api/jobs /api/job /api/job/events /api/job/cancel
- runs      /api/run /api/explore /api/gen（启动长任务）
- device    /api/devices /api/screenshot /api/device/elements|key|scrcpy
- review    /api/review GET/POST

纪律：本包**不得** import web_server（单向依赖：web_server → web_api）。
"""
