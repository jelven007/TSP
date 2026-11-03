# 历史中转镜像与缓存清理

日期：2026-10-31。承接用户对 TSP、rustdx、AIGC 的本地清理要求，
本次处理 TSP 专用 Colima 镜像存储和三个项目使用的开发包缓存。

## 实际结果

| 对象 | 清理结果 |
| --- | --- |
| 镜像 | 删除3个 `tsp-runtime-transfer` 中转镜像，11个镜像降为8个 |
| Docker 构建缓存 | 210条缓存、Docker报告4.968 GB，清理后0条、0 B |
| 包/下载缓存 | 74个闲置目录，删除时合计55,018个文件；删除前分配块统计约2.63 GiB |
| VM 数据磁盘 | 分配占用22,254,530,560字节降至7,106,580,480字节，减少约14.11 GiB |
| 合计口径 | VM 数据磁盘减少量与包缓存分配块合计约16.74 GiB |

逐镜像身份、缓存路径、删除前和删除时统计、验证标记见
[机器清单](cleanup.json)。

分配块按 `st_blocks × 512` 统计，包缓存只计删除前没有外部硬链接的文件，
不把镜像、共享层和构建缓存逻辑大小相加。APFS克隆、快照和并发写入可能影响
整盘可用空间增量。本次合计不包含启动既有 VM 时自动稀疏化的减少量、
VM 系统盘的额外回收，或新清理记录占用。
`fstrim` 报告的27 GiB可丢弃空闲区间也不等于新增释放27 GiB。

## 删除依据与保留范围

已删除的中转镜像对应 `f4d8c11b766b`、`5b273174f2e2`、`bac72d9a83c0`，
其下载和重建用途已完成；相关正式运行镜像仍保留。
现存8个镜像包括 v0.4.37～v0.4.39 的3个 compute、3个 runtime-host，
以及原已存在的 v0.4.38、v0.4.39 两个 cell 镜像。
没有补造原本不存在的 v0.4.37 cell 镜像。

缓存范围为闲置的 npm 下载与 npx、pip HTTP、uv 包/索引、Cargo 下载/源码、
node-gyp 头文件、Colima 基础镜像下载和 TSP 小型 Cargo 构建缓存。
执行前核对 `lsof` 与进程启动路径，保留仍使用的两个 npx MCP 目录；
运行所需的 Node、pnpm、Rust 工具链、项目虚拟环境和实际安装依赖保持。
初次删除遇到缓存元数据变化后暂停；重新检查活动文件，再继续同一白名单。
uv wheels索引在清理期间新增54个文件，实际删除数按删除时清单计，
不沿用初始54,964个文件的预览计数。

`.cache/tsp-dev/TSPP/backups` 实际保存项目备份，未按缓存名称删除。
主行情、财务数据、PostgreSQL、用户策略、报告原件、冻结证据、
v0.4.37～v0.4.39恢复材料、身份/SMTP配置及 Git 历史保持。

## 本次核验

- 暂时启动既有 `tspp-compute` VM，用 Docker API 核对原库存：
  11个镜像，0个容器，0个卷。未删除容器或卷。
- 对3个中转镜像执行指定 ID 的 `docker image rm`；
  `docker builder prune --all --force` 清理闲置构建缓存。
  `docker image prune --force` 额外回收0 B，没有把保留的带标签镜像纳入全量 prune。
- 清理后8个保留镜像的 ID 和标签逐项保持，容器/卷清单保持，
  74个候选缓存目录均不存在，保留的活动 npx 目录仍存在。
- `colima ssh --profile tspp-compute -- sudo fstrim --all --verbose`
  回收空闲磁盘区间；随后恢复 VM 原先的停止状态。
  `colima list --json` 确认 `tspp-compute` 为 `Stopped`。
- TSP 既有解释器实际导入 `numpy`、`polars`、`psycopg`、
  `tsp_rustdx_native` 成功；Node 24.22.1、pnpm 9.10.0、
  项目 Rust/Cargo 1.89.0 命令成功。
  初次验证误用了不存在的 `rustdx` Python包名，随后按已安装模块名核验；
  pnpm 初次缺少 Node PATH，指定已有工具目录后通过，没有重新安装依赖。
- 三个仓库 HEAD 与清理前一致，新增本清理记录前的 Git 状态逐字节一致；
  本次没有提交、推送、修改源码或递增应用版本。

`PYTHONDONTWRITEBYTECODE=1 make check` 通过：87个必需路径、35份当前文档、
八模块需求、0.4.39版本和 diff 空白检查。
`PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_docs.py --include-reports`
通过：209份 Markdown、报告链接和保留需求基线。
本次为本地缓存清理验证，不作为重新部署、完整业务测试或行情验收。
