# rustdx_complete

当前源码版本：<!-- version:project -->2.0.4<!-- /version -->。数字由主线 CI 同步；注册表历史发布事实另列。

通达信同步 TCP 行情、本地 day/lc/gbbq 文件解析、复权、指标、日历及日期化交易规则的 Rust 库。包名 `jelven-rustdx-complete`，导入名 `rustdx_complete`；Rust 2024，最低 Rust 1.89。

```toml
[dependencies]
rustdx-complete = { package = "jelven-rustdx-complete", version = "2.0.0" }
```

2.0.0已发布到crates.io，发布源码及包摘要见仓库docs/archive/release/PUBLICATION_20261025.md；源码集成固定提交和锁文件。日历所有入口返回Result，财务/心跳返回新类型，迁移说明在仓库docs/release/MIGRATION.md。接口文档通过 `cargo doc -p jelven-rustdx-complete --no-deps` 构建。

库采用同步 I/O 和有界线程，TCP 主连接、池、订阅及重连共享网络预算。配置须在首次初始化前完成。证券键保留市场和代码，源量/价格/金额按协议与周期解释，未知值不得补零；分页检查完成状态，批量逐项处理结果，订阅按同步状态恢复。

26 个同码入口覆盖不等于全部业务验收。F10/退市市场2、近期分时日期、真实盘中主动推送及其他增强契约有单独边界。

源码仓库中的[集成规则](https://github.com/jelven007/rustdx/blob/main/docs/development/APPLICATION_INTEGRATION.md)、[完整命令参考](https://github.com/jelven007/rustdx/blob/main/docs/reference/COMMAND_API.md)和[项目文档](https://github.com/jelven007/rustdx/tree/main/docs)随源码版本维护。MIT 许可及原始版权声明见本包 LICENSE。
