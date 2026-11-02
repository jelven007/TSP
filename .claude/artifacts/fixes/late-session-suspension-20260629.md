# 尾盘走强2026-06-29停牌状态缺失

状态：FIXED，本地安装及实际Compute验证完成。日期：2026-11-02。
范围仅本地v0.4.42，不提交、推送、加号或上线。

## 现象与复现

用户报错：`2026-06-29: 尾盘走强: 股票池中 1/1561 只缺少指定分钟价格或昨收`。
失败任务`e064672600b64ee68d4190fd45369524`使用公共快照
`144a166f24474f6bb5339c13395f4cab`，研究区间2026-06-29～10-30，自定义`custom_mvxxcp88`。
只读加载该任务源码/覆盖/信号及原快照，同源引擎复现1561池和完全相同诊断。
缺失股票为陕鼓动力`601369.SH`：6月26日raw_close为9.05元，6月29日没有任何分钟K；
池停牌未知计数1。6月29日～7月10日连续10个交易日均无分钟K，
6月26日和7月13日各有240行，不能把实际停牌误作价格采集缺口。

## 根因与证据

H1真实取数遗漏：已排除。停牌公告及复牌公告与分钟空窗一致。
H2历史停牌证据缺失：已确认。现有`instrument_status/history.parquet`只保存名称、ST、上市身份，
陕鼓动力首个状态从2026-10-24开始；没有独立历史停复牌字段。策略虽勾选exclude_suspended，
`suspension.annotate`正确保留无当日行情的未知状态，导致它留在完整池。
尾盘广度随后拒绝缺14:30价的未知成员，这是数据完整性保护，不能用删除未知成员绕过。

官方初始公告：
https://www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-06-27/601369_20260627_GAG5.pdf
（2026-06-27，临2026-026，6月29日开市起停牌）。

复牌公告：
https://paper.cnstock.com/html/2026-07/11/content_2242688.htm
（2026-07-11，临2026-033，7月13日开市起复牌）。

## 有界修复方案

沿既有`instrument_status`公共数据目录新增停复牌事件文件，保留原上市/ST历史文件。
事件记录symbol、生效日、公告日、停牌布尔和来源URL；只消费信号日之前已公开且已生效的事件，
同日缺精确公告时点的记录不提前使用，未来复牌不泄漏到历史。
真实行情价/量仍来自原K线，不生成停牌日虚构OHLC。

Repository缓存加载该小型文件；Screener和分钟回放沿现有`pool_universe`传递同日状态证据。
纯`strategy/suspension.annotate`接收证据，按symbol与证据日期附加停牌状态，独立于旧价格行日期，
股票池继续按范围/流通市值和显式exclude_suspended设置处理。
新增文件加入既有研究输入指纹，公共发布沿既有目录白名单形成新不可变快照，
不修改失败任务的旧快照或历史结果。

## 验证与交付

- [x] 回归先红：真实引擎的`1/2`缺价诊断连续3次失败；修复后16项新增回归通过。
- [x] 事件时点、复牌边界、排除开关、数值开关及既有ST/上市身份兼容。
- [x] 未勾选排除或无证据仍保留池成员并报告缺价；仅状态证据不扩充股票范围。
- [x] 临时将证据消费反转为空后，新增回归重新失败；不stash或覆盖并行工作。
- [x] 同源真实6月29日：1561→1560，原诊断消失，上涨占比48.2692307692%，买点0。
- [x] 停复牌证据入本地受控源和新公共快照，匹配7个API/Worker/Compute文件并优雅重启。
- [x] 策略、信号、监控、模拟账户/订单/成交和自动规则11组签名保持；受控配置及前端指针保持。

本次没有新运行真实行情收益回测，不交付新的五资金/逐月/压力结论或Excel研究表。

## 实现与回归

`app/instrument_suspension.py`校验独立事件文件和时点，并通过既有pool_universe传递；
Repository按inode/mtime/size缓存，原子替换后重读。`suspension.annotate`附加精确当日证据，
当前真实成交/当日状态优先；旧价格行不被替换，未知仍未知。
`daily_pool`不把仅状态行当股票范围，Screener/盘前池/MinuteSignalReplayer共用消费，
`data_release.research_input_fingerprints`加入文件。没有新公开插件、数据库迁移或版本递增。

V-1：`pytest tests/test_instrument_suspension.py tests/test_daily_stock_pools.py
tests/test_late_session_strength.py tests/test_instrument_history.py tests/test_data_release.py
tests/backtest/test_minute_backtest.py tests/backtest/test_morning_backtest.py -q --tb=short`
本轮170项通过，其中新回归16项。
V-2：回归反转证据消费重新报`1/2`缺价。
V-3：新增/纯池/回放/指纹模块Ruff通过；Screener与Repository相对安装前运行备份无新增诊断，
既有诊断分别11→10、51→50，不声明全库lint清零。
V-4：匹配Compute构建和build-manifest校验通过；真实隔离Compute验证池1560/买点0。
V-5：`make check PYTHON=backend/.venv/bin/python`结构、文档、版本和diff whitespace通过。

## 本地实际结果与安装身份

| 输入 | 完整池 | 停牌已知/未知 | 上涨占比 | 尾盘买点 | 诊断 |
| --- | ---: | --- | --- | ---: | --- |
| 原失败公共快照 | 1561 | 0/1 | 不可认证 | 0 | 原1/1561错误 |
| 新公共快照 | 1560 | 1/0 | 48.27% | 0 | 无 |

当日14:30上涨数753/1560，未达到60%，因此无入场为正常结果。
只重放6月29日入场信号，不计算收益。6月26日/7月13日分别240分钟，
6月29日/7月10日为0分钟；证据状态分别未知/复牌、停牌/停牌，未来复牌未泄漏。

新公共release：`b831094bb2a34144bb9565722818c65c`，
补录数据release：`ada96a0f272a4f7aa32dca7b510ef29d`。
文件SHA256：`79b0c8e9505cba5ffb11e92b3a7540e43cd68f5d0e9b70ea061cf0746fde6279`。
本地Compute：`sha256:a8192826fbf2b11b396364e60e60be423668affb978140a83e5df4973f9be932`。
前端仍为`morning-profit-final-20261102`，应用保持0.4.42。
七个运行文件与工作区hash一致；API live/ready为ok，Compute ready，8个受监督服务存活，
管理员实际登录及策略配置查询通过。Platform ready=true，但实时enriched更新造成全局
data_release generation warning，状态degraded；本次固定公共快照绑定及计算已验证正常。
不把实时发布警告掩饰为全角色全绿。

备份在`~/.cache/tsp-dev/TSPP/backups/suspension-20260629-20261102`，
签收在`~/.cache/tsp-dev/TSPP/suspension-20260629-20261102`。
工作区证据：[发布](late-session-suspension-20260629/publication.json)、
[旧新单日与Compute](late-session-suspension-20260629/verification.json)、
[健康与文件身份](late-session-suspension-20260629/live-check.json)、
[用户状态保留](late-session-suspension-20260629/preservation.json)、
[原运行lint对照](late-session-suspension-20260629/lint-baseline.json)。
恢复代码须先停监督进程，核对当前7个文件、镜像和配置身份，仅恢复对应备份与Compute，
不回退用户配置、账本或旧公共快照。此次事件仍为受控源数据，不补造停牌行情。

原失败任务后续查询已不再可见，本次没有删除或改写研究任务。
最终对照使用签名验证保持的当前源/覆盖和原失败公共快照，准确复现原错误；
初始根因复现确实使用当时冻结任务输入。不能称原任务仍存在或已成功重跑。
旧不可变公共快照仍缺事件文件，重新研究须新建任务选取新输入。

## 当前策略与部署边界

选：沪深主板、排除ST/停牌、前交易日流通市值50～500亿元，
前日涨幅0.5%～6%，20根已完成日K均成交额≥1亿元；完整池仅范围/流通市值，
其余条件只参与选股，涨跌幅100%高值评分保持。
买：14:30涨幅0.4%～4%、上涨占比≥60%、7个1分及4个5分MA5严格递增。
卖：实际买入批次精确下一交易日振幅达到6%或10:30截止；其他固定退出/最长持有关闭。
资金、最大持仓、总仓位和费用/滑点沿回测页面，未修改交易或信号参数。
原源revision4/覆盖revision14保持，本次只改变已知停牌的入池状态。
未执行完整2026-06-29～10-30收益研究，也未部署线上。

## Pattern analysis

实际`git grep -n suspension -- backend/app backend/tests`核对同类路径：
既有S02/overnight的suspension_events用于实际持仓等待复牌，不承担通用池广度，
此次没有合并或改写该契约。通用池状态消费只有Screener和MinuteSignalReplayer两入口，
均已接入同一证据函数；纯广度evaluate保留严格缺价保护。
