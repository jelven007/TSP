# rustdx v2.0.4 接入融合验证

日期：2026-10-28。应用版本保持 `0.4.36`。
起始项目提交：`1c630cc2d13cf3e2ed545b9a4d9ff5b340d224af`。
上游标签：`v2.0.4`；固定提交：
`c4c0273456874898aed50447fd900521d9ffd3b8`。

## 实施范围

- 固定上游101个生产Rust源码文件及MIT许可证，以本地精确`2.0.4`路径依赖构建。
  `UPSTREAM.json`、`SOURCE-SHA256SUMS`、`PATCHES.md`和`build.rs`共同拒绝来源漂移。
- 保留v2.0.4新增的Windows排队文件锁和历史状态校验；没有引入上游CLI、runner或运行数据。
- 桥接升级为`0.2.0`、`POOL_API_VERSION=3`，进程限制为8条业务连接和1条独立探测连接。
- 行情节点使用证券数量探测；财务节点使用实际心跳探测，均要求三轮完整成功。
- 共享证券精度目录，移除ETF二次倍率修正；K线分页绑定节点、连接身份和北京时间观察日。
- 最新财务快照显式使用legacy字段兼容现有股本/上市信息；专业财报保留清单、MD5、ZIP和DAT解析链。
- 冷精度目录及全市场报价使用批量绝对时限，直接证券目录和F10使用分页绝对时限；
  单socket传输仍受`RUSTDX_TIMEOUT`限制，不按页刷新总截止时间。
- Provider七项能力、内部schema、本地派生周期、用户配置和应用版本保持不变。

## TDD证据

1. v2.0.4身份测试先在v2.0.3实现上失败：安装身份不匹配且缺少
   `cache_windows_lock.rs`。同步最新源码和身份后，37项身份/构建清单测试通过。
2. 真实节点首次报价先以`TimedOut`失败。慢目录本地服务器复现“总分页超过2秒、
   每次传输低于2秒”的问题；修复后冷精度目录回归通过。
3. 直接证券目录同样先以`TimedOut`失败；建立120秒分页父任务后回归通过。

## 完成的验证

- vendor身份检查：revision `c4c0273...`，107个实际编译/身份文件；
  核心摘要 `9c5082e8a43d217dc1f2ba2558545648830a953b84e83962d8bc0660f90955d5`；
  补丁摘要 `6ffce99f5e2d31e12673e3a9eb391c5953750471e734f7cab1d49213f47e17bd`。
- 上游核心：203项单元测试通过；55项文档测试通过、18项按上游标记忽略；
  2项compile-fail文档测试通过；严格Clippy和rustfmt通过。
- PyO3桥接：7项Rust测试、严格bridge-only Clippy及rustfmt通过。
- Python聚焦回归：Provider、原生池、财务、同步、能力路由、分钟路由、
  公司行为、桌面和Compute打包等274项通过。
- 工具测试136项通过；仓库目录、文档、版本和diff检查通过；增量Ruff无新增诊断。
- `uv lock --check`通过；本地wheel身份为
  `jelven-rustdx-complete 2.0.4 / bridge 0.2.0 / 8业务+1探测`。
- Windows GNU目标交叉`cargo check --locked`通过，覆盖v2.0.4新增Windows缓存锁源码。

## 公开节点只读验证

行情节点读取`600519.SH`、`000001.SZ`和`588200.SH`报价成功，价格分别为
`1330.75`、`11.31`、`0.872`；ETF未发生二次除十。`600519.SH`日K返回3条，
legacy财务字段可读。财务节点读取`tdxfin/gpcw.txt`成功：8577字节、148行。
本轮实际峰值为3条业务连接及1条探测连接，无业务数据写入。结构化结果见
[live-smoke.json](live-smoke.json)。

## 跨域失败修复

最初的116项失败全部来自本地测试入口继承正式`.env`和平台配置目录。CI、Docker和
Release质量门原本均显式使用`/dev/null`、standalone模式和临时数据目录，Makefile
遗漏了相同隔离。修复本地入口后：

- 原16个失败文件：`209 passed`；
- 完整后端：`4663 passed / 162 skipped / 0 failed`，180项warning；
- 前端：84个测试文件通过，ESLint和生产构建通过；
- 工具链：136项通过，仓库、文档、版本和diff检查通过。

根因、红绿反向验证和回归测试见
[backend-test-environment-isolation.md](../../.claude/artifacts/fixes/backend-test-environment-isolation.md)。

## macOS本机构建

按Release依赖范围构建了Apple Silicon冻结应用，打入`pywebview`、
`polars-runtime-compat`和rustdx原生桥接。验证结果：

- `TSP.app`为arm64，版本`0.4.36`，bundle id为`com.tickflow.stockpanel`；
- 运行清单1958个文件，校验无changed、missing或added项；
- 隔离standalone smoke产生`DESKTOP_SMOKE_TEST_OK`和
  `DESKTOP_AUTH_BYPASS_SMOKE_TEST_OK`，修复了发布workflow继承外部配置的问题；
- DMG只读挂载、Applications链接、深度代码签名和镜像校验通过；
- 本地候选`TSP-macos-arm64-v0.4.36-local-candidate.dmg`为240293106字节，
  SHA-256为`fe893253450a178541dcfe9fc4e75756382afa1d1a5dde4a3e1b72b8719ab858`。

## 未通过与边界

当前工作树包含尚未提交的融合改动，运行清单明确记录`source_dirty=true`。正式
`build_manifest.py seal`按规范拒绝，错误为
`release requires a clean, matching source commit`。因此上述DMG是经过运行验证的
本地候选，不是可上传GitHub Release的已封印正式资产。

本机没有Docker daemon、Homebrew或`create-dmg`，未构建Docker、Windows或Linux
发行产物，也未执行正式全市场P95、完整历史财报初始化、提交、推送、部署或服务重启。
运行中的旧进程不会自动加载新wheel；正式发布仍需将改动纳入干净固定提交，由CI构建
多平台产物并生成外部manifest，再进行隔离数据、代表交易窗口和回滚演练。
