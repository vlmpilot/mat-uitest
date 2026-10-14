# demo-app（自建被测靶场 App）

spike 与对抗集的靶场：控件 id 固定可控、可故意埋 bug。完整 Gradle 工程已就绪（Java + View 体系，无业务依赖）。

## 构建与安装

```bash
# 方式 A：Android Studio（推荐，自带 Gradle/JDK）
#   打开 examples/demo-app 目录 → 等 Sync 完成 → Run

# 方式 B：命令行（需 JDK 17 + Gradle 8.2+，或用 Studio 生成的 wrapper）
cd examples/demo-app
gradle :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## 页面与控件 id

| 页面 | 控件 | id |
|---|---|---|
| 登录页（启动页） | 账号输入框 | `com.insta.demo:id/username` |
| | 密码输入框 | `com.insta.demo:id/password` |
| | 登录按钮 | `com.insta.demo:id/login_btn` |
| | 错误提示文本 | `com.insta.demo:id/error_text` |
| | 查看公告（⚠️ 带缺陷版上点它会进**白屏页**） | `com.insta.demo:id/announce_btn` |
| 公告页 | 标题（**带缺陷版上没有**——整页空白） | `com.insta.demo:id/announce_title` |
| | 正文（同上） | `com.insta.demo:id/announce_body` |
| 列表页（登录成功后） | 欢迎文案（含账号名，如"欢迎，alice"） | `com.insta.demo:id/welcome_text` |
| | 结果计数（"共 N 项"） | `com.insta.demo:id/item_count` |
| | 搜索框 | `com.insta.demo:id/search_box` |
| | 空状态提示（"没有找到相关结果"，默认隐藏） | `com.insta.demo:id/empty_hint` |
| | 长按上下文菜单（"删除"，默认隐藏） | `com.insta.demo:id/ctx_menu` |
| | 导出报表按钮（⚠️ 带缺陷版上点它会**崩溃**） | `com.insta.demo:id/export_report_btn` |
| | 导出结果（默认隐藏；无缺陷版上点导出后显示「已导出 N 条记录」） | `com.insta.demo:id/export_result` |
| | 列表项（20 条静态数据，"蓝牙耳机 Pro"固定首位） | `com.insta.demo:id/item_text` |
| 详情页（点列表项进入） | 标题 | `com.insta.demo:id/detail_title` |

行为：账号任意、密码 `pass1234` 登录成功；否则 `error_text` 显示"账号或密码错误（次数）"并保持登录页。搜索框输入即过滤（TextWatcher）。

## 故意埋的 bug（对抗集用：缺陷检出 / 自愈 / 归因评测，**勿修**）

> 这些是**产品缺陷探针**的靶子（探针集属内部评测资产，不在本仓库发布，下同）：
> 探针断言的是**正确行为** ⇒ 在带缺陷版上失败 = **真的检出了缺陷**。
> **配对臂**：内部的变体生成器（`make_variants.py`）的 `v09-defect-free` 会把下面**全部**
> 缺陷修好，同一批探针在它上面应**全过**——以此证明"失败是因为缺陷"，
> 而不是用例本身不成立。

| # | 缺陷 | 位置 | 探针 |
|---|---|---|---|
| 1 | 空关键词搜索 → 列表显示 `undefined`（文案 bug） | `ListActivity` 的 `OnEditorActionListener` | `defect-empty-search-undefined` |
| 2 | 密码连错 3 次后登录按钮**仍可点击**（无禁用态） | `MainActivity` | `defect-login-btn-should-disable` |
| 3 | 筛选后计数仍显示**全量 20**（应为过滤后的条数） | `ListActivity.updateChrome` | `defect-list-count-wrong` |
| 4 | **长按列表项无任何反应**（正确行为：弹出上下文菜单） | `ListActivity` 的 `OnItemLongClickListener` | `defect-longpress-no-menu` |
| 5 | 搜索框超 10 字符被**静默截断**且无提示 | `ListActivity` 的 `afterTextChanged` | `defect-search-truncated` |
| 6 | 搜索**无结果时不显示**空状态提示 | `ListActivity.updateChrome` | `defect-no-empty-hint` |
| **7** | 点「导出报表」**直接崩溃**（主线程未捕获异常） | `ListActivity` 的 `export_report_btn` | `defect-crash-export`（执行级） |
| **8** | 「查看公告」页**渲染空白**（白屏） | `BlankActivity` | `explore-blank-page`（**探索级**） |

⚠️ **#7 与 #8 的判据都不在 DSL 断言上**：
- **#7 崩溃**：判据是**失败分类必须是 `app-crash`**（`runner.classify_failure` 靠失败步 logcat 里的
  `FATAL EXCEPTION`）⇒ 由内部的期望判定器自动核对（该判定器不在本仓库发布）；
- **#8 白屏**：判据是 **`explore` 的 `white-screen` finding**（`_screen_suspicious`：
  截图**灰度极差 < 6** 判可疑，再按均值分白/黑屏）——DSL 断言**分不清"空白页"和"走错页"**
  ⇒ 由内部的探索探针判定器核对（该判定器与探针集都不在本仓库发布）。

⚠️ **#8 的入口刻意放在登录页**：`explore._candidate()` 只把**可点击**控件纳入候选，而 `EditText`
不是可点控件 ⇒ 它**登不进去**，放列表页这一页就永远探不到。

> 📌 **#4 这条探针有一段值得记住的历史**（2026-10-06）：
> 配对臂一度把它**否掉**——它在「带缺陷版」和「无缺陷版」上**都失败**，说明它测的不是这个缺陷。
> 真因是**引擎的 `long-press` 本身没生效**（被当成普通点击）：u2 默认 0.5s 不够，
> 实测翻转点在 **1.0–1.2s 之间**（不是 Android 文档里的 500ms——u2 的 `jsonrpc.click`
> 实际按压比请求的短）。引擎已改为 **2.0s**，探针**已补回**。
> ⇒ **配对臂先发现 → 修引擎 → 补回探针**，这条闭环是配对臂价值的完整例证。

⚠️ **改动本 App 时必须同步内部变体生成器（`make_variants.py`，不在本仓库发布）的 `_DEFECT_FIXES`**：
那里的修复片段要求**精确命中且唯一**，漂移会**报错**（不会静默跳过）。

改版脚本（专测 AI 自愈，不属于上面这份缺陷清单）：发布 v1.1 时把 `item_text` 的 id 改名 / 登录页加全屏广告弹窗。

## 与 benchmark 的对应

t1 登录（含错误分支 t4）、t2 搜索过滤、t3 列表进详情——全部 res-id 定位；t0 冒烟不依赖本应用（系统设置）。
