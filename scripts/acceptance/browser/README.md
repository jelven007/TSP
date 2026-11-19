# 真实浏览器验收

使用 Playwright Chromium 测试编译后的前端和真实 FastAPI 路由。
配置启动 `accept_local_web --serve`，只绑定 `127.0.0.1:39019`，
每次使用临时数据和固定虚构账号；拒绝复用已有监听服务。
外部行情／定时任务由既有隔离 fixture 替换，浏览器禁止访问外部 origin。
应用版本检查由本地固定响应替代，并在证据中记录该替换；初始化向导设为已完成。
此套件不是 PostgreSQL 多租户隔离、真实交易窗口或生产 P95 的证明。

从项目根目录完成依赖安装及前端构建，再执行：

```bash
pnpm --dir frontend build
npm --prefix scripts/acceptance/browser ci --ignore-scripts --no-audit --no-fund
npm --prefix scripts/acceptance/browser exec -- playwright install chromium
npm --prefix scripts/acceptance/browser test
```

默认 Python 为项目的 `backend/.venv/bin/python`。可通过 `TSP_TEST_PYTHON`
指定绝对解释器路径；`TSP_BROWSER_OUTPUT` 指定证据目录，默认写入系统临时目录下的
`tsp-browser-acceptance`。不将 cookies、认证 storageState 或生产数据保存为测试种子。
失败 trace 包含虚构账号测试请求，CI 附件只保留本 fixture 的产物。

每个 viewport 使用独立浏览器上下文，共用本次临时后端，顺序执行、零自动重试。
覆盖五条旅程：行情／自选分组与搜索添加、数据画像／同步完成记录、
策略历史日期／空结果保护、真实研究回测／成交结果、模拟账户创建／冻结／解冻。
拒绝路径包含未登录、退出后访问、零资金创建、未选策略运行。
输出 JSON/JUnit、截图、失败 trace、API 状态、浏览器错误与警告及交互耗时。
各旅程记录一次浏览器冷缓存和两次热缓存入口；后端保持同一 fixture，不表示后端冷缓存。
单个步骤包含定位、动作和断言等待，不等同首屏绘制或服务端延迟。
阶段结果不能跨设备混为 P95；正式性能验收必须固定设备、数据、网络并重复采样。

CI 复用 `frontend` 作业的同提交 `dist`，不重跑其测试和构建；
浏览器作业与后端质量门共同阻断正式标签和镜像提升。
