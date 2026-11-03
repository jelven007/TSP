# 尾盘建仓启动后一分钟检查 Implementation Plan

> **For agentic workers:** 按本计划在隔离工作目录内联执行，先测试失败再实现，
> 每个阶段以实际命令核验。提交和线上发布按用户授权范围处理。

**Goal:** 将自定义尾盘建仓升级为可设置启动时间、启动后一分钟检查六项条件，
并确保14:31才可计算、双周期数据截至14:30；5m四值14:30>14:25>14:20>14:15。

**Architecture:** 复用现有模板，新增内部可选`evaluation_delay_minutes`作为
新旧定义时间契约标记。共用实际检查时间函数贯通评估、池观察、注入与回放；
N个MA5值的既有算法保持，编辑器保存新语义。

**Tech Stack:** Python/Polars/FastAPI、React/TypeScript、pytest/Vitest。

---

## Task 1：隔离基线与测试红灯

Files:
- `backend/tests/test_late_session_strength.py`
- 新定向延迟测试可置于`backend/tests/test_late_session_start_delay.py`。

- [x] 从main `4453ecc48d66`建立隔离工作目录，保留主工作区已有修改。
- [x] 新测试以`definition(evaluation_delay_minutes=1)`及`bars(TARGET)`：
  `completed_through=TARGET`无信号，`TARGET+1分钟`命中a/b/c且广度60%。
- [x] 让14:30与14:31广度相反，断言读取14:30完成截面，不缩小全池分母。
- [x] 验证1m七个MA5值截止14:30；5m四值取14:30/25/20/15。
- [x] 验证缺14:30、未来14:31、平值、缺中间K线、秒级时间、最晚启动。
- [x] 执行pytest，保存业务断言失败原因；旧六字段测试仍保持旧时点。

## Task 2：模板时间契约

Files:
- `backend/app/strategy/late_session_strength.py`

- [x] 参数接收内部延迟0或1；缺字段返回0，新默认定义/HTTP保存写1。
- [x] 共用`evaluation_time(sig, day)`计算`datetime.combine(day,time)+delay`。
- [x] `pool_cutoff`使用实际检查时点，仍保留多个信号及fallback最早观察约束。
- [x] `bar_time`返回启动数据标签；报价/广度/1m截止用该标签，5m向下对齐；
  提前观察直接无结果，未来行不参与。既有严格N值MA5算法保持。
- [x] 运行新定向测试转绿和全部旧模板测试。

## Task 3：信号标记与回放/撮合

Files:
- `backend/app/strategy/engine.py`
- `backend/app/services/late_session_replay.py`
- `backend/tests/test_late_session_start_delay.py`
- `backend/tests/test_instrument_suspension.py`

- [x] 引擎新脉冲仍附着启动数据行，携带`entry_evaluated_at`实际可计算时间。
- [x] 本地回放返回计算分钟和`bar_time`，不伪造14:31数据行。
- [x] 以现有完整池/API回放夹具验证请求子集仍读取全池，返回14:31。
- [x] 分钟回放事件不得早于14:31；signal_close保留14:30参考价，
  next_minute_open取14:31真实开盘，不多延迟到14:32。
- [x] 以公告时点夹具验证新模板可观察14:30之后、14:31之前的公告；
  旧定义仍按14:30观察，不借未来公告。

## Task 4：正式保存与编辑器

Files:
- `backend/app/api/signals.py`
- `frontend/src/lib/api.ts`
- `frontend/src/lib/customSignals.ts`
- `frontend/src/components/signals/CustomSignalDialog.tsx`
- 相应后端API/Vitest测试。

- [x] 新HTTP保存对该模板缺省延迟补1，保留其他模板请求不变；
  历史加载不自动改写定义。
- [x] 前端类型增加内部可选字段，默认值写1；编辑保存统一升级为1。
- [x] 字段顺序：启动时间、+1分钟上涨占比、涨幅上限、涨幅下限、
  +1分钟1m MA5数量、+1分钟5m MA5数量。
- [x] 标签、摘要、说明分开启动数据/可计算时刻；说明1m截至14:30及四个5m示例、
  七个1m值和严格递增；旧定义保存升级提示准确。
- [x] 六项修改保存、重开回显、界限/错误/取消与出场模板回归通过。

## Task 5：文档、范围与运行验收

- [x] 同步需求/架构/开发/测试/运营/运维中实际受影响契约；
  不把尚未安装写成已部署，不改版本号。
- [x] pytest覆盖模板、池公告、分钟回放、早盘收益兼容、对应API；
  前端相关Vitest、TypeScript/Vite构建及增量lint。
- [x] 将已核验改动带回用户工作区时逐文件确认原内容无并行修改，
  仅应用本任务diff，保留已有文档变更。
- [x] 本地运行源码/前端/Compute按现有工具完整安装；切换前备份、
  安装后检查配置持久化、健康、真实Compute与页面保存/刷新。
- [x] 当前活动尾盘建仓仅显式升级时间语义为1，其余六参数值、
  s02出场6%双时间及实际账户保持。
- [x] 出具验收记录和当前规则，区分已有回测与本次行为检查。

## 执行签收

用户纠正后的双周期数据截止为14:30，可计算时点为14:31；旧错误候选不再使用。
222项后端、19项前端相关回归、构建/lint/文档和差异格式检查通过。
完整本地运行单元799源码文件、381 Compute应用文件和275静态文件逐文件核对，
构建清单和同镜像生成行情夹具通过。活动入口仅升级延迟标记，
桌面/手机保存刷新以及第二次完整重启持久化均通过；原配置、出场和账户签名保持。
版本仍v0.4.46；本次没有新收益回测、提交、推送或线上时间修正部署。
完整证据见`reports/late-session-start-plus-one-20261103.md`；页面版本更新查询的远端404/CORS
已记录，未修改该独立行为。
