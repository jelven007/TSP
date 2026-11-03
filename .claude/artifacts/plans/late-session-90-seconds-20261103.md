# 尾盘建仓启动后90秒 Implementation Plan

> **For agentic workers:** 在本隔离工作目录内联执行，先红后绿，按实际验证签收。
> 当前用户已授权更新90秒及沿用本地安装流程；不重复批准，不提交/推送/线上部署。

**Goal:** 默认14:31:30确认14:30数据，双MA5/广度/涨幅统一截止启动时间。
**Architecture:** 复用内部分钟延迟标记1.5和既有两个时点；秒级确认与整分钟成交分开。
**Tech Stack:** Python/Polars/FastAPI、React/TypeScript/Vitest、既有完整本地Compute。

**执行状态（2026-11-03）：已完成。** 下列内容为执行前任务清单；
实际签收见文末和[验收记录](../../../reports/late-session-90-seconds-20261103.md)。

## 1. 先写业务测试并观察失败

- [ ] 新建`backend/tests/test_late_session_ninety_seconds.py`。
  用既有`bars/definition/DAY/TARGET/POOL`夹具，`definition(evaluation_delay_minutes=1.5)`；
  验证`evaluation_time==TARGET+timedelta(seconds=90)`，`bar_time==TARGET`，
  completed_through为14:31:05/14:31:29无信号、14:31:30命中a/b/c。
- [ ] 加14:31反向行仍仅广度60%、缺14:30拒绝，UTC observed_at同口径；
  七个1m值和四个5m值沿用已有完整窗口负例。
- [ ] 引擎在14:31:29不注入，14:31:30注入真实14:30行，
  `entry_evaluated_at`和`_collect_signal_hits.evaluated_at`均为14:31:30。
- [ ] API回放先读取完整池再输出a，返回`time=14:31:30/bar_time=14:30:00`；
  保存缺标记新定义写1.5，加载旧0/1不改写。
- [ ] 分钟回放提供14:31开盘10.18、14:32开盘10.22，
  新1.5命中trigger_time14:32/signal_time14:31:30/价格10.22；
  缺14:32不成交，signal_close参考价保持但事件14:32及确认时间明确。
- [ ] 新时间边界14:58合法、14:59和15:00拒绝；旧1的14:59仍合法。
- [ ] 修改`test_late_session_start_delay.py`中HTTP新保存期望1.5，其他旧1测试保持。
- [ ] 前端`customSignals.test.ts`和`CustomSignalDialog.test.tsx`覆盖默认1.5/14:31:30、
  旧0/1准确摘要及保存升级、14:58上限、六字段顺序和90秒标签。
- [ ] 执行后端定向pytest与前端定向Vitest，记录业务断言红灯，不把导入错误当复现。

## 2. 最小实现并转绿

- [ ] `backend/app/strategy/late_session_strength.py`默认1.5；
  延迟只允许数值0/1/1.5，排除bool，保留旧缺省0；1.5时间上限14:58。
  `evaluation_time`仍用timedelta(minutes=delay)，不改`bar_time`和MA5算法。
- [ ] `backend/app/api/signals.py`新保存缺省标记改为1.5。
- [ ] `backend/app/backtest/minute_replay.py`确认时间格式保留非零秒；
  available_at非整分钟向上对齐后查真实成交分钟，不向下截断。
  signal_close新半分钟确认保留`signal_time`，旧整分钟模式兼容。
- [ ] `frontend/src/lib/api.ts`延迟类型0|1|1.5，注释90秒；
  `customSignals.ts`按总秒计算，默认1.5，新旧摘要区分真实时点和升级提示。
- [ ] `CustomSignalDialog.tsx`保存1.5、标签说明90秒、旧0/1编辑提示、
  启动时间max14:58且校验一致；不加用户字段。
- [ ] 运行新窄测试绿灯，再覆盖旧模板/延迟/停复牌/分钟回放/早盘出场相关模块。

## 3. 文档、完整本地安装及签收

- [ ] 同步`docs/requirements.md/architecture.md/secondary-development.md/testing.md/operations.md/release-operations.md`
  当前60秒段为90秒；旧报告保留，生成新的90秒报告。
- [ ] 在隔离目录执行后端相关pytest、前端相关Vitest/build、变更lint、增量Ruff、
  check_docs及git diff --check。没有新收益回测。
- [ ] 对`source-before.json`逐文件验证用户主工作区仍为捕获基线，
  仅将隔离中相对捕获基线的新90秒差异应用回主工作区，不覆盖已有改动。
- [ ] 复用上一轮`/private/tmp/tsp-late-session-start-acceptance-20261103/install.py`的
  完整复制和清单流程，BASE取当前活动`...-late-session-completed-local`，
  新候选目录90秒另建，不复用已存在输出。
  构建全app/静态和匹配Compute，保留依赖、原生扩展及受控.env，
  两侧所有应用/静态逐文件哈希与清单一致后才停止完整supervisor并切换。
- [ ] 活动`custom_mvxxcp88`入口仅升级内部标记1.5，用上轮state.py防并发/状态签名方法，
  保留六参数、出场6%/10:30/10:45、策略、监控、纸面账户/订单/成交/持仓。
- [ ] 同镜像生成行情夹具验证90秒秒边界及14:32真实下一开盘；
  live broker健康/受限describe及实际镜像参数独立验证。
- [ ] 已安装Playwright检查真实`http://127.0.0.1:3021/signals`，
  新模板默认、活动编辑同值保存/刷新、desktop1440/mobile390、说明14:31:30/14:30；
  记录已知GitHub版本查询404/CORS，与目标接口失败分别标注。
- [ ] 保存后再完整重启，登录/读取活动标记1.5，全部状态签名和完整服务健康验证。
- [ ] 支持资产/截图和最终报告保存用户根workspace；中间脚本/原始受控状态留临时目录。
  最终报告及源文件提供本地文件链接。最终说明90秒窗口、六参数、
  出场/仓位保持、实际验证、本地生效、无新回测且本次未上线。

## 实际签收

- [x] 先红后绿：旧实现后端9项、前端2项失败；90秒定向58/5项通过。
- [x] 秒边界、14:30数据缺失、未来行、完整池、双MA5窗口和旧0/60秒兼容通过。
- [x] 独立回放双时间、引擎秒元数据、14:32真实开盘与缺行情拒买通过。
- [x] 相关后端322项、前端11项通过；主工作区落地111项复验通过。
- [x] 生产构建、变更ESLint、增量Ruff、文档及diff检查通过；六类文档更新。
- [x] 23项原有改动围栏通过，仅回写90秒差异和本次新文件。
- [x] 完整本地安装：381后端/275静态文件一致，依赖/原生环境/受控配置保持。
- [x] 当前活动信号升至1.5，六参数和出场6%/10:30/10:45保持；状态签名一致。
- [x] 活动Compute镜像因果夹具、真实Broker健康和请求验证通过。
- [x] 桌面1440/手机390真实保存刷新通过，截图已发布用户工作区。
- [x] 保存后完整重启、登录和1.5持久化通过；八个服务就绪。
- [x] 新报告及支持资产已发布；无收益回测、提交、推送、版本递增或本次线上部署。
