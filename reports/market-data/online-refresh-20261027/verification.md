# 本地最新源码发布与线上 K 线清理

日期：2026-10-27（北京时间）。用户授权：将本地最新环境更新到线上，
删除1分K、5分K、10分K、15分K、日K、周K等有效数据。

最终状态（北京时间02:24:20）：ECS已升级v0.4.32，六周期有效K线已清空；
登录、业务与秘密保留、127秒观察及两个新巡检周期均已核对。
上线后设置接口再次开启自动同步，保留该新设置。基础服务就绪，全功能因缺指标未就绪。

## 已完成的源码及产物核对

- 冻结46文件并完成逐文件复审，PR #28已合并。
- PR源码提交：85d753b96aa44e80764c77a376996df4bcd792b4；
  squash提交：6fbf27f19b89c89c4843942bb7b598bc2d4fe4b9。
- CI自动版本v0.4.32，main、标签及ci-revision：
  4da5f30c9b5165612d78b573c142612f3fe25395。
- main CI 40370758749和Docker CI 40371792120成功。
- main实测后端4610通过/9跳过、前端364、工具134、原生桥6。
  本地先前后端4465通过/154跳过，负例在旧代码上47失败；
  构建、lint、文档目录检查和最终diff检查通过。环境不同，计数不混用。
- Runtime index：
  sha256:d4ebf07c54e20fcc5d96695520999efa58887dfd9f12d2c401f3d115f52d4059。
- amd64 child manifest：
  sha256:56fd2a062c30daa409eda4d012d54fa0220ce303c89f61704039d0a8e59e77c0。
- amd64 config：
  sha256:e46d2f8772f81ba3d3f8aa13cb2212931607e4f114cb17cdd9d497125cdded42。
- 提交归档SHA256：
  0d1334b5599d29611ff37ca4acd4dedf5fdcde1951d8827ebf46c4575d142845；
  上传到ECS后已核对相等。
- 本地工作区已同步main/v0.4.32，三方同步无冲突，559个既有研究文件hash保留；
  本地行情及运行进程保留。见[同步证据](reconciliation.json)。

## 线上执行与验收

| 项目 | 当前结果 | 证据 |
| --- | --- | --- |
| ECS固定Runtime/同源码Compute | 通过 | [产物](artifacts.json)；623文件清单相等，native/vectorbt成功 |
| 双库备份恢复及业务对比 | 通过 | [备份](backup.json)；双库schema v6，恢复核对项与源相等 |
| 12个K线目录备份 | 通过 | 3760文件SHA256相等；[归档验证](validation.json) |
| 六周期源/current清零 | 通过 | [清理](clear.json)；更新三个generation及空公共current |
| 新版本启动与外网版本 | 通过基础就绪 | [切换](promotion.json)、[外网](external-after.json)；非指标依赖正常 |
| admin登录、配置、业务保留 | 首轮及末轮通过 | [末轮审计](post-audit-final.json)；随后自动同步变更单独记录 |
| 桌面/窄屏页面 | 升级前后通过 | [升级后浏览器](browser-after/result.json)，1440px/390px |
| 两个新健康检查周期 | 清零/基础检查通过 | [观察](online-watch.json)，126.9秒；02:22:29和02:23:29的新巡检均无K线回写 |

ECS直接拉取两个来源分别45秒超时，保留旧服务后采用离线镜像。
本地Docker默认inspect返回多架构index元信息；按明确linux/amd64检查，
与单平台归档及注册表config/rootfs相等，复用已下载归档继续传输。
latest未用于部署身份。升级前[外网健康](external-before.json)仍为v0.4.31，全部检查正常。
离线归档477998080字节，完整SHA256两端相等；增量传输用时352秒，
实际发送377108270字节。Compute准备和native/vectorbt验收成功后才停写。
临时清理容器最初以Docker原始env-file读取带引号SMTP端口，导入配置时校验失败；
此时12个源目录已移动，空current尚未发布。修正临时容器为只读dotenv解析，
应用导入前降权至10001，从发布阶段继续，未重复移动目录或修改私有配置。
只读配置验证、修复范围及实际清理成功分别见clear-resume-probe/receipt和clear JSON。

## 清理范围与使用影响

清理有效股票、ETF、指数的日K与enriched数据、股票/ETF分钟K，
以及5/10/15分钟和周K，共12个目录。对应原始K线、缓存generation、
数据release和公共current须同步；画像行数为0、最早/最晚日期为空。

清理、启动和首轮审计时自动同步为false。
北京时间02:19:29设置接口PUT `/api/settings/preferences/data-sync-auto`成功，
随后只观察到该字段由false变为true；验证浏览器已于02:18:46退出，未写设置。
来源身份不推断，按仓库规则保留上线后的主动配置操作，见
[变更证据](user-auto-sync-change.json)。当前自动同步为true，后续定时任务可能重新生成K线。
缺少K线后，依赖指标与历史行情的研究、监控、模拟交易等功能暂不可用；
这属于本次清理的预期影响，不能称为全部功能就绪或行情初始化完成。
行情初始化应为pending，重新采集由用户决定。
开启自动同步后，外置分钟巡检的“自动同步关闭且空K线”例外不再生效，
巡检full-health为FAIL；API仍ready，只有indicator_cache不可用。
这项全功能未就绪状态如实保留，不通过放宽线上检查或填充数据消除告警。

用户当前注册/找回/SMTP配置、账号、PostgreSQL业务、研究引用须保留。
旧不可变研究输入及受控备份保留；此次是有效数据清空，不是全盘历史文件销毁。
未发布桌面平台。

## 恢复与审查记录

匹配备份：`/opt/tsp-full/backups/pre-v0.4.32-4da5f30c9b51`，
含双库dump、私有配置、源码/外置操作脚本、12目录和旧镜像。
原K线另保存在该目录的`retained-originals`。
Runtime实际Docker ID为amd64 child 56fd2a062c30，Compute为c6e6c4765f38，
完整值见artifacts.json。旧镜像归档RootFS/labels已比对，SHA256见validation.json。
发生非预期故障时先停止提升并调查，
恢复需匹配代码、Runtime/Compute、私有配置、双库和源K线/generation/release/current；
不能只退版本或自动重新采集历史。

具体发布门见[发布验收](release-readiness.md)，源码复审见[复审记录](source-review.md)。
本记录与对应JSON仅保存非秘密证据，真实邮箱授权码不写入报告。

版本/运维基线在本地记录本次部署实测，属于发布后的运维记录；
固定发布镜像及版本保持本次CI分配的v0.4.32。工作区并行研究保留。
