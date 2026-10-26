# Windows 冻结程序原生退出阻塞

> Status: FIXED
> Mode: --deep
> Severity: blocker
> Last updated: 2026-10-26

## 现象与预期

正式发布工作流40261574429中，v0.4.28安装后的管理员、HTTP和真实
EdgeChromium DOM验证成功，窗口已关闭，进程仍未在240秒内退出。
该版本未正式发布或部署，标签保持原提交。
预期是应用有界清理及日志关闭后进程真实结束，保留0/1退出码。

## 诊断与根因

诊断工作流[40263587108](https://github.com/jelven007/TSP/actions/runs/40263587108)
固定v0.4.28提交846a6c7484aa62df569a6fabc91417d8a265e10e，仅在诊断构建中
注入独立文件阶段标记、线程栈与两种退出方式比较，不封装正式发布资产。

| 假设 | 结论 | 实际证据 |
| --- | --- | --- |
| 后端清理或单实例锁释放阻塞 | 排除 | `stop_server_returned`、`release_lock_returned`、`main_returned code=0`均存在 |
| `logging.shutdown`等待日志锁 | 排除 | `logging_shutdown_returned`存在 |
| CRT原生退出触发DLL卸载阻塞 | 确认 | 最后阶段为`os_exit_enter`，150秒仍存活；同构建改用`TerminateProcess`后22.15秒以0退出，包含新的HTTP/管理员/GUI成功标记 |
| GUI未真正渲染或自动关窗 | 排除 | 两轮均有真实EdgeChromium DOM成功和窗口关闭日志 |

Windows的`os._exit`仍进入CRT/系统退出路径，调用已加载DLL的
`DLL_PROCESS_DETACH`；相关卸载代码可能等待已终止线程持有的锁。
[Microsoft ExitProcess契约](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-exitprocess)
明确描述该死锁及`TerminateProcess`不通知DLL卸载的区别。
此次证据定位到原生退出边界，不断言某一供应商或WebView DLL的具体锁实现。
诊断工作流因ProcDump遗留退出码为失败；比较结果与日志单独核对，不能把工作流本身称为成功。

## 修复与兼容

仅在冻结Windows桌面入口中，于`main`和日志清理完成后调用当前进程的
`TerminateProcess`。显式声明64位句柄和退出码类型，调用失败明确抛出WinError。
其他冻结平台保留`os._exit`，源码启动保留`sys.exit`与正常atexit。
不增加配置、公共接口、schema或插件能力，不改变GUI/安装/卸载验收预算。
重要任务仍应完成后关闭；有界清理的既有事务和原子文件边界保持。

## 回归与验证

`backend/tests/test_desktop_process_exit.py`加载测试DLL，其原生进程卸载回调
永久等待；子进程执行真实入口尾部并先落盘应用清理标记。必须结束并保留0/1返回码。
源码按UTF-8读取，避免Windows默认编码在到达应用入口前失败造成误判。

原生[40264566927](https://github.com/jelven007/TSP/actions/runs/40264566927)实测：

- V-1：旧入口连续三次各2 failed；失败前实际落盘`APPLICATION_CLEANUP_DONE`。
- V-2：修复候选的完整退出模块6 passed。
- V-3：仅撤回退出修复，2 failed，再现原生卸载阻塞。
- V-4：恢复候选后完整模块6 passed；候选与正式源码尾部AST完全一致。
- V-5：本地桌面退出、打包和构建清单模块45 passed、2个Windows专属跳过；
  `make check`通过31份文档、87项目录和0.4.28版本一致性，`git diff --check`通过。
- 原生测试在非Windows明确跳过，已接入Windows原生CI；正式安装包须在合并后的新版本重新验收。

首次两个诊断回归40263780305/40264156920未通过完整证据门：
Windows默认源码编码导致应用清理标记缺失。它们不计入上述三次可靠复现。

## 模式检查与发布边界

实际搜索`os._exit`、`sys.exit(main())`及`_stop_server`：
桌面入口是本问题的唯一入口；另有watchdog独立故障退出和回测子进程退出，
它们不走桌面WebView生命周期，本次不修改。
现有非daemon线程测试只覆盖Python关闭行为，未覆盖Windows DLL卸载，故v0.4.28仍失败。
新增原生回归补足这一区别；不以诊断、单测或源码标签代替正式Release和生产升级。
v0.4.27/0.4.28均未正式发布或部署；新版本只由main CI自动分配。
