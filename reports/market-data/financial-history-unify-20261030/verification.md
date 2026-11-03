# 财务历史快照本地与线上一致性修复

2026-10-30已完成实际数据补齐。数据总览存储卡片中的“财务历史快照”在
本地与线上均显示**148 文件、70.7 MB**。源目录、公共不可变副本、API返回、
桌面1440×1000、窄屏390×844及刷新后的页面均已核验。

本轮检查整个财务目录的文件名、字节数、SHA256、Parquet内容哈希、元数据引用、
观察时间与实际历史回放，不以只分开统计作为修复完成标准。

| 项目 | 本地修复前 | 线上修复前 | 修复后两端 |
| --- | ---: | ---: | ---: |
| 历史目录文件数 | 113 | 83 | 148 |
| 历史目录字节数 | 63,967,168 | 47,036,365 | 74,104,316 |
| 历史目录大小，API | 61.00 MB | 44.86 MB | 70.67 MB |
| 内容快照数，Parquet | 36 | 26 | 45 |
| 真实观察事件数 | 36 | 26 | 52 |
| 当前财务表 | 5文件/18.15 MB | 5文件/18.15 MB | 5文件/18.15 MB |
| 整个财务目录 | 118文件 | 88文件 | 153文件/93,140,394字节 |

根因为两端独立同步形成了不同的历史集合。上一轮修复仅使当前财报统计可比较，
仍留下113/83历史文件的实际差异，未完成用户要求。本次采用两端已验证历史的并集，
本地新增35文件、线上新增65文件；两端各原子替换12个冲突元数据或latest指针。
所有原Parquet及观察JSON文件字节保留，没有删除历史，也没有把合并时间冒充原观察时间。

148个历史文件由45个Parquet、45个快照元数据、52个观察事件、5个latest指针、
1个合并来源记录组成。各表快照/观察事件分别为：metrics 23/26、income 6/7、
balance_sheet 5/6、cash_flow 5/6、shares 6/7。45个内容快照均通过标准化内容哈希校验。
同内容元数据取两端真实最早first_observed_at，latest取并集中最新真实观察；
两端原元数据冲突、文件清单和来源保存在revisions/merge-provenance.json。
publication_time_verified未被升级为true。

新版本回放时间线使用两端实际观察事件并集；这会扩充单端原有可见事件，
不宣称新时间线与合并前每一个查询结果完全相同。旧公共不可变版本财务文件
已单独验证未被改写，冻结到旧版本的研究输入保持。首次真实观察之前仍返回空表。

完整财务文件清单的SHA256在两端一致：
`5c4f48987a81a927198e05841aae0f49d5720952a63eea8436b99aef370252cc`。
该摘要对有序的相对路径、字节数及逐文件SHA256清单计算；不是压缩包摘要。
五张当前表仍为625,956条、每表5,222标的，逐文件SHA256保持；
metrics/shares已有API查询内容摘要也与修复前相同。

验证证据：

- 真实修复前两端文件一致性断言连续3次失败；补齐后通过，切回只读原输入再次失败，
  再验证实际运行目录通过，见[parity-regression.json](parity-regression.json)与
  回归断言（附件保存在受控归档）；输入目录由FINANCIAL_PARITY_LEFT/RIGHT显式传入。
- 合并/应用6项测试通过：A→B→A回放、原文件和观察时间保留、坏快照拒绝、
  当前财报不同拒绝、源目录并发变化拒绝、候选被改写拒绝及幂等。
- 每端调用已部署get_financial_revision验证52条真实观察事件，
  首次观察之前为空；源/公共副本均与完整已审计并集一致，
  见[local-verified.json](local-verified.json)与[online-verified.json](online-verified.json)。
- 使用显式工作区PYTHONPATH和`uv run --no-sync python -m pytest`执行
  test_data_status_storage、test_financial_auto_sync、test_financial_shares、
  test_rustdx_financial、test_fuyao_financial、test_assistant_financials_order：38项通过。
  首次使用pytest控制台入口组合运行出现3失败/35通过，改用上述明确源码路径复跑
  相同测试集合38通过；未通过删断言或修改业务实现消除失败。
- Chrome Playwright访问本地与公网/data，核对页名、实际卡片文字、页面非空、
  无框架错误覆盖层、控制台无错误；reload后148文件/70.7 MB保持，
  桌面和窄屏均验证，见[browser-check.json](browser-check.json)。
- 管理员登录与readiness全部检查正常，API各财务统计字段一致。
  公共发布后最初一次线上查询命中原60秒storage缓存；TTL后及浏览器验收均返回新值。
- 所有本次维护脚本Ruff及仓库git diff --check通过。

浏览器插件未提供可调用工具，使用现有Chrome和缓存Playwright环境。
第一次uv在线解析依赖因PyPI连接超时失败；改用`--offline`缓存后验证成功。
未修改项目依赖。首次线上发布脚本调用系统Python缺少starlette；
改用同镜像/app/.venv/bin/python后正常发布并完成全部校验。

数据应用前短暂停Platform writer并核对原审计清单，财务完整备份逐文件校验后才写入；
发布采用既有PublicPublisher，不改写旧公共版本，不将可变源文件直接硬链接到公共副本。
暂停进程/容器均已恢复；线上健康与公共版本维护timer均恢复active。
本地后续原有1分K任务仍正常运行，未修改其任务或设置。

| 环境 | 本轮公共版本 | 发布前公共版本 |
| --- | --- | --- |
| 本地 | 498540dd547a43d0b48966c2f607bbcc | 00cf8df8993241b69aa7f9e800ee9144 |
| 线上 | 02b306e7de014a9081e321694506464c | 7670c380eaa64b998ca790eecfccbb81 |

源数据备份：

- 本地受控缓存：financial-history-unify-20261030/local-backup/financials-before.tar.gz；
  SHA256 `dc76f67963e328e00536422a4638e37384844e3dc1a02f8999c7f96f884113a0`。
- ECS：/opt/tsp-full/backups/financial-history-unify-20261030/financials-before.tar.gz；
  SHA256 `4712ca744d546be3ca91ea81128df985649fb6a50ba8f3d1dbf992b38857f155`。

备份未放入源码仓库。若需要恢复本轮数据，先停止财务写入、确认无后来新增观察，
校验备份并恢复匹配的财务目录，再通过正常发布器生成公共版本；不要直接覆盖旧公共版本。
已有合并记录和两个只读原输入也保留在受控缓存/导入目录中。

应用仍为0.4.38，沿用既有统计修复镜像
`sha256:6420311935048783ca64a1b9475824f8e8987b8e5c74f9854766ea032987447d`。
本轮为真实数据补齐及验收文档更新，未提交、推送或手动递增版本。
它证明本次两端财务集合一致；以后两端独立采集产生的新观察仍可能演化，
不宣称已建立永久跨环境自动同步，也不据此认证其他行情已全部初始化。

页面证据：[本地桌面](local-desktop.png)、[线上桌面](online-desktop.png)、
[本地窄屏](local-mobile.png)、[线上窄屏](online-mobile.png)。
已验证的审计、合并、应用、发布、回放脚本及测试原样保存于evidence（附件保存在受控归档），
原财务数据与完整备份留在受控环境。
