# 构建清单与离线升级备份

构建清单把源码提交、应用版本和文件 SHA-256 关联起来；升级备份保存二开差异及
人工合并材料。工具不修改索引、合并分支、恢复运行代码或重启服务。
Python 3.9+ 可运行，使用标准库；本地修改不递增应用版本。

## 构建身份

| 产物 | 清单位置与内容 |
| --- | --- |
| Docker API | `/app/build-manifest.json`：app、static、tiers、VERSION、锁文件、原生 wheel 与核验脚本 |
| Compute | 先验证基础镜像清单，再以当前源码重建清单；构建层先删除旧 app，避免已删除模块残留 |
| 桌面 | 包内 `source-manifest.json`：参与打包的源码指纹、Git SHA、工作区 dirty 状态 |
| 桌面安装包 | `<安装包名>.manifest.json`：安装包 SHA-256、完整运行目录文件索引和源码身份 |

`format=1` 的清单包含 `scopes`、每个文件的类型/大小/哈希和整个文件索引的
`files_sha256`。内部相对符号链接只记录链接目标，外部或悬空链接拒绝。
不扫描运行数据、`.env*`、Git、日志和缓存。Docker 的依赖闭包由 `uv.lock` 固定，
清单不声称覆盖整个基础操作系统或安装后的所有第三方依赖；原生 wheel 保留并核验。

Docker CI 把实际通过质量门的 SHA 传入 `SOURCE_COMMIT`，与镜像 label 相同。
本地构建不传时记录 `commit=null`，不会把未知源码冒充已提交版本。
传入 SHA 是构建者声明，容器构建中没有 Git 可供独立认证；应结合干净检出和镜像
digest 使用。桌面从实际 Git 检出读取 SHA 和 dirty 状态；发布封装检查拒绝未知、
dirty 或与 workflow SHA 不同的源码。散列不是数字签名，清单应从可信发布渠道取得。

本地示例：

```bash
python3 scripts/build_manifest.py create --root . --profile source \
  --version-file VERSION --git-root . --output .dbg/source-manifest.json

docker build --build-arg SOURCE_COMMIT="$(git rev-parse HEAD)" -t tsp:candidate .
docker run --rm --network none --entrypoint python tsp:candidate \
  /app/build_manifest.py verify --root /app --manifest /app/build-manifest.json
```

`create` 不覆盖已有输出。源码配置只索引构建输入；额外部署文件可通过显式
`--include <相对路径>` 创建清单。桌面外置运行清单的根是包含 `TSP/` 或 `TSP.app/`
的目录；在该目录核验，不能以 `.app` 内部子目录为根。

`verify` 返回 `changed`、`missing`、`added`：无差异退出 0，有差异退出 2，
输入无效、越界或读取失败退出 1。读取时验证内容哈希，不依赖 mtime 缓存。
清单最多 100,000 文件；此操作是显式离线工具，不进入启动、HTTP 或行情热路径。

## Git 二开升级材料

```bash
# 只读预演，保留原命令
python3 scripts/upgrade_check.py <目标Tag或commit>

# 可选：写入一个尚不存在的备份目录
python3 scripts/upgrade_check.py <目标Tag或commit> --bundle .dbg/upgrade-review
```

`report.json` 记录共同基线、当前/目标完整 SHA、双方改动路径、可识别的文本冲突、
排除路径及各备份文件哈希。`sources.zip` 分别保存 `base/`、`current/`、
`target/`、`index/`、`working/`，二进制内容原样保存；删除通过对应版本无条目表示。
文件名含空格或中文可用。三个 patch 分别是基线至当前提交、当前至暂存区、
暂存区至工作区；未跟踪的源码在 `working/` 中保存。

备份限定已声明的源码/文档目录；数据、密钥常见路径、构建输出、日志和符号链接
不作为源码材料。它不是用户数据备份，也不能识别误写在普通源码中的秘密；
生成物含私有二开代码，不应提交或自动上传。最多 5,000 路径、256 MiB 内容，
超限或已有未解决索引冲突会失败。源码、索引或 HEAD 在备份期间变化时，不生成
完成标记。备份目录必须新建；POSIX 使用 0700，Windows 仍受目录 ACL 管理。

三方文本预演只针对**已提交的树**；未提交修改虽已备份，仍应在临时分支形成提交后
重新预演。二进制重叠、重命名语义和金融口径都需人工复核。没有文本冲突不表示
兼容。需要恢复时，在当前提交的独立检出中先核对哈希，再依次审核并应用暂存/
工作区 patch，检查 `working/` 中的新增文件；不要把归档整体解压到运行目录。

## 已安装产物的差异备份

保留旧清单及可信候选清单，把安装目录作为只读输入，在升级前运行：

```bash
python3 scripts/build_manifest.py backup --root <旧安装目录的清单根> \
  --manifest <旧清单.json> --candidate <候选清单.json> \
  --output <新的备份目录>
```

按旧清单的范围扫描本地变更，并生成 `local-files.zip`、两份清单和 `report.json`。
目录必须在被扫描范围外，拒绝符号链接输出路径；单次备份最多 256 MiB。
本地删除在报告保留 tombstone；本地符号链接仅保存目标文本，不自动重建链接。

| 分类 | 含义 |
| --- | --- |
| `candidate_unchanged` | 候选保留旧基线，本地有修改 |
| `local_added` / `local_deleted` | 本地新增 / 本地删除且候选仍是旧基线 |
| `matches_candidate` | 本地内容与候选一致 |
| `conflict` | 本地与候选均偏离旧基线，须人工处理 |

`report.json` 最后写入，只有 `complete=true` 才代表备份流程完成。扫描或复制期间
文件变化会失败；未完成目录可保留用于排错，不能当作恢复依据。它不是文件系统
事务快照，升级时应先停止源码写入。旧/候选原始文件可按记录的 SHA 从可信源码或
原始安装包取得；只有哈希的清单无法还原旧文件字节。

生产恢复通过二开源码合并、完整回归和重新构建固定镜像/安装包完成。数据与配置按
[发布流程](./release-operations.md)单独备份和恢复，切换后验证 readiness、
登录、核心业务与源码/镜像身份。工具不提供在线覆盖程序或一键重启入口。
