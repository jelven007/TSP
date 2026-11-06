# Bug: 端点TCP额度测试误用服务器清理计数

> Status: FIXED
> Mode: --deep
> Severity: 发布质量门阻断
> Last updated: 2026-11-07

## Symptom / Expected

PR #47 CI `42296137506`后端为5,641通过、9跳过、1失败、184 warnings。
失败位置是`test_distinct_groups_share_the_physical_endpoint_budget`：
模拟服务器`peak=10`超过断言9。正确契约是同一物理端点跨任务组共享
8条业务TCP额度，额外独立探测最多1条；额度用尽须拒绝，关闭后可重获。

## Reproduction

仅在首个业务池8条连接就绪后，以Event阻挡模拟服务器收到EOF后的清理，
保留真实原生客户端和三轮顺序资格探测，不mock业务连接或额度。
原`server.peak <= 9`断言连续3次失败，均为11；增加真实TCP观测后仍失败。
命令：在隔离DATA_DIR、`TICKFLOW_ENV_FILE=/dev/null`及冻结Python环境中运行
`pytest tests/test_rustdx_task_quotas.py::test_distinct_groups_share_the_physical_endpoint_budget -q`。

## Hypotheses & diagnosis

| 假设 | 事前估值/置信度/顺序 | 预测与证据 | 结论 |
| --- | --- | --- | --- |
| 业务端点额度被第二任务组绕过 | 40%/中/2 | 应出现第9条业务TCP或成功xdxr；实际请求被拒绝，端点及network active始终8，第二组active=0 | 排除 |
| 三轮资格探测未串行释放额度 | 30%/中/3 | 应同时出现两条以上探测；实际probes峰值1，客户端TCP峰值9 | 排除 |
| 服务器处理线程滞后被计作客户端TCP | 30%/高/1 | 应出现server peak>9且真实TCP<=9；tagged观测为server peak=11、client peak=9、完成后client=8 | 根因确认 |

## Root cause

`_Server._accept`增加live/peak，`_handle`在EOF和连接关闭之后的finally才减少live。
客户端已释放顺序探测的TCP并释放许可时，服务器处理线程可以尚未执行finally；
后续探测被接受后，与这些旧线程一起累计成10或11。该计数不是客户端同时持有的TCP数。
沿原生调用链核对：`ManagedClient`先释放Client再释放业务Permit；
`probe_one_group`先释放TcpStream再释放探测Permit，端点探测额度为1。

## Fix / Regression test

只改`backend/tests/test_rustdx_task_quotas.py`这一用例。
保留延迟服务器清理，按psutil中的唯一local/remote TCP对计数，
避免macOS reader/heartbeat克隆FD重复计数；观测峰值必须到9且不得超过9。
同时检查端点/进程业务额度8、第二组0、探测active<=1、额外请求拒绝、
完成后真实TCP8、关闭首池后第二组可获取且端点额度1，最终服务器清空。
不修改Rust、上游固定身份、生产额度或其他用例。

## Verification

- V-1：原计数断言在Event场景下连续3次失败，11>9。
- V-2：真实TCP观测明确为峰值9、结束8；旧计数断言仍失败。
- V-3：修正计数断言后定向用例1通过，5.97秒，Ruff通过。
- V-4：用apply_patch只恢复旧计数断言，原生TCP断言通过后旧断言再次11>9；
  随后精确移除该旧断言恢复修复，未使用stash或破坏性Git操作。
- V-5：`pytest tests/test_rustdx_native_pool.py tests/test_rustdx_task_quotas.py -q`
  共26通过、0失败，50.00秒（含最终回归、跨进程共享与关闭回收）。
- V-6：最终Ruff通过；`rg`检查测试中唯一tagged锚点0匹配，临时诊断已移除。
- V-7：bits-unit-test-gen的prepare已执行；其`utree`辅助总结程序未安装，
  按要求实际调用flush返回127。pytest日志/JUnit和本记录正常保存，
  不把辅助命令称为通过，不将该缺失误作产品测试失败。
- 远程CI重跑、主线质量门与生产部署分别签收，不将本地验证冒充远程通过。

## Pattern analysis / Follow-ups

`git grep -n 'server.peak\|\.peak <= 9' -- backend/tests`在修复前共6处正式断言。
其他5处（`test_rustdx_native_pool.py`原195/378/401/420行及
`test_rustdx_task_quotas.py`原92行）同样依赖服务器处理线程，
可能受清理时序影响；本轮没有失败证据，不扩大本次修复。
其中运输超时用例还需要独立说明已断开客户端但仍在处理的服务器请求，
不统一替换或放宽上限。

## RCA

- 引入：`2d5d0130`，2026-10-30，该用例首次加入的提交（路径Git历史核对）。
- 漏检原因：本地正常调度下服务器EOF清理通常快于下一次探测，未覆盖清理滞后。
- 预防：保留Event构造的滞后场景，真实socket与许可状态联合断言；
  必需CI失败先定位，不通过单纯重跑或放宽上限放行发布。
