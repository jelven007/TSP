# 2026-10-30 最新整合验证记录

本轮起点为0.4.38。当前正在核对最终整合候选，应用版本由main CI分配。
范围及六类文档映射见[发布评审](review.md)。

## 当前运行基线

2026-10-30通过现有HTTP入口只读核对：本地与ECS均为0.4.38，
管理员登录成功，六项功能ready；原16只自选、2个分组保持。
本地存在活跃任务`d5f7f7d7e6`，线上无活跃采集任务。切换前必须重新检查并排空。
真实秘密配置仅在受控运行环境及备份保存。

## 最终候选实际验证

各层分别统计。新鲜命令运行于2026-10-30，后端使用独立DATA_DIR、
`TICKFLOW_ENV_FILE=/dev/null`、standalone、`POLARS_MAX_THREADS=4`，
明确PYTHONPATH指向本轮工作区；运行环境原生桥接为0.3.0/API4。

| 验证 | 命令 | 实际结果 |
| --- | --- | --- |
| 后端全量 | `python -m pytest tests -q` | 4,683 passed、162 skipped、173 warnings，292.62秒 |
| 发布工具 | `python -m pytest tests/tooling -q` | 136 passed，70.17秒 |
| 前端全量 | `pnpm test` | 85文件、456项通过，11.90秒 |
| 前端质量 | `pnpm lint`、`pnpm build` | 通过；已有大chunk提示仍存在 |
| 桥接 | 指定项目Python的`cargo test --locked` | 10 passed |
| 核心 | `RUSTDX_SKIP_INTEGRATION_TESTS=1 cargo test --locked` | 203单测、55 doctest与2 compile-fail通过；18 doctest ignored |
| Rust静态 | 核心/桥接`cargo fmt --check`与`cargo clippy --all-targets -- -D warnings` | 均通过 |
| Python增量 | `scripts/check_ruff_delta.py --base 5b273174 --ruff ...` | 16文件，历史56项保持，新增0 |
| 缓存类型 | `mypy app/services/public_query_cache.py --follow-imports=skip` | 1文件通过 |
| 工程/文档/版本 | `make check` | 87路径、32文档、8需求模块与0.4.38一致 |
| 冻结锁 | `uv lock --project backend --check --offline` | 125依赖通过 |
| 上游身份 | `scripts/vendor_rustdx.py`、`python -m app.plugins.rustdx.client` | 107文件、core/patch哈希和API4通过 |

首轮前端454通过、1失败：目录初次搜索防抖在250毫秒后覆盖用户页码，
新增假时钟用例稳定复现，修复后完整回归通过。原实现失败证据保留在受控目录。
把原实现放入独立临时目录再次执行同一用例，确认仍失败；
完成失败、修复通过、隔离反转失败的回归闭环。
桥接首轮误选系统Python3.9导致缺失链接库，改为项目Python3.12后通过；
核心首轮真实公网固定节点4项超时，不将其称为通过。随后按仓库CI已有设置
执行确定性核心测试；公开节点验证仍以独立样本和发布产物检查为准。
完整文件Ruff检出4项既有历史问题，仓库规定的增量门禁确认本轮新增0。

候选界面使用已安装Playwright/Chrome，工具发现未提供Browser插件。
桌面1440×1000、手机390×844检查指数日期回显和滑块整数范围；
候选指数响应为显式模拟，本地常驻后端仍0.4.38，不能认证正式产物。
首次测试脚本保存拦截路径不一致导致本地间隔短暂变为10/9秒；
依据08:13备份的3秒值与之后无用户修改的API日志，已只恢复该值并校正拦截。
重新验证前后配置必须一致，截图和完整记录留在受控发布目录。
重新运行通过：初次指数请求省略开始日，之后回显2020-11-02；滑块依次产生
10、9的模拟保存请求，两次实际HTTP读取仍为3秒，手机无横向溢出，
页面无运行时异常。生产日/周/月完整历史仍须升级后实际HTTP验收。

## 待完成的发布与升级验证

- [x] 最终候选源码测试、前端测试/lint/build、原生测试/fmt/clippy与文档/版本检查。
- [ ] PR质量门、main自动版本、标签及双架构固定产物。
- [ ] 本地实际备份恢复、升级、登录、六功能、原生分组、真实任务与正常重启。
- [ ] ECS实际备份恢复、升级、登录、六功能、原生分组、真实任务与正常重启。
- [ ] 已合并分支删除及并行工作保留核验。

已知历史数据缺口沿原持久结果如实保留；发布完成不能替代历史初始化验收。
