# 本地项目目录TSPP重命名为TSP

北京时间2026-10-30 21:34完成。

本地实际工作目录已由`/path/to/home/Documents/TSPP`改为
`/path/to/home/Documents/TSP`。应用名、前后端包名和GitHub远程仓库本来已使用TSP，
本次主要统一本地目录名称。

## 兼容路径

| 路径 | 实际指向 |
| --- | --- |
| `/path/to/home/Documents/TSPP` | 新工作目录`/path/to/home/Documents/TSP` |
| `/path/to/home/.cache/tsp-dev/TSP` | 既有缓存目录`/path/to/home/.cache/tsp-dev/TSPP` |
| `/path/to/home/.local/share/tsp-dev/TSP` | 既有运行及数据目录`/path/to/home/.local/share/tsp-dev/TSPP` |

以上均为符号链接，不是重复复制的数据。原虚拟环境脚本、历史文件链接、关联工作树和运行路径
可以继续解析；新TSP名称也访问同一份已有缓存和数据。

## 验证结果

- 重命名前后Git HEAD一致：`bac72d9a83c03890cbb954e0e13782b8421d6073`。
- 重命名前后原有暂存/未暂存/未跟踪状态完全一致；本记录目录为随后新增。
- `.git/config`、`.git/HEAD`、`.env`、版本、README、前后端包定义和启动脚本的内容哈希保持一致。
- 工作目录、运行目录和数据目录的原inode保持一致，未复制或重建行情数据。
- 17个关联工作树均可解析其Git目录。
- 新路径中的后端虚拟环境可运行并导入Polars，版本为1.44.1。
- 本次没有停止或重启服务。检查时本地3018和3011端口在重命名前后均不可达，
  因而没有将其描述为“启动或登录验证通过”。

## IDE使用

在IDE中重新打开`/path/to/home/Documents/TSP`，项目树将使用TSP目录名。
当前通过旧TSPP路径打开的窗口可能继续显示旧名称，但其文件仍可通过兼容链接访问。

完整结果见[重命名验证](verification.json)。
