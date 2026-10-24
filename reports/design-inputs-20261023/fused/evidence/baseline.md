# 本地证据索引

核对日期：2026-10-23。当前源码基线：`cc837b95f471e5b299bf5e8618332d500bff7e3a`；应用版本：0.4.17。

下列摘要针对工作区文件，工作区已有未提交修改；提交 SHA 不能替代工作区内容哈希。报告依据是本次读取的文件版本。路径相对原仓库，仅作为证据标识；交付包不复制私有数据或整份源码。

当前技术栈、角色和需求事实已纳入融合方案正文。历史验证结果保留日期，不作为本次新增架构实测。

<a id="current"></a>
## 当前架构与能力

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `docs/architecture.md` | 30625 | `5a1abd4c3bc77a7582bc91b6cf089f68dd0f1e12671b16465a86a2c8ac50f471` |
| `docs/current-capabilities.md` | 8966 | `b603f812bfbf4e28a2de42fe32c7dd0a9485ddd09944203237f1a7a221c8835e` |
| `deploy/full/compose.yml` | 7348 | `7773d9f069416e70813e7eeb8828c06b9bc17c986f6fc35cdb195b608ad43a77` |

<a id="requirements"></a>
## 需求基线

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `docs/requirements.md` | 88071 | `97e10d6d513f4b82cf79e502ba687d36cde7a78a2a895d384315a3cafbbb6995` |

<a id="contracts"></a>
## 开发与数据契约

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `CONTRIBUTING.md` | 34543 | `241b522182e4eb08e35cc960b4f8a43fddf46151ab6ec62a2fc1f7bdfd1d25db` |
| `VERSION` | 8 | `87faa9c6eca98a20b09bea9848bb01fa3f3f1c854dacbe72a2c71fbbaa1416be` |

<a id="extensions"></a>
## 二次开发

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `docs/secondary-development.md` | 20366 | `40d166345f1d570698900e85fd322bb3efbd665a46b5bcd482d0cd97fac1d4df` |

<a id="clickhouse"></a>
## ClickHouse 八表设计

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `deliverables/ClickHouse十年历史表设计/设计说明.md` | 23616 | `468d55cb6ac686d71a1ee742dee68cf90beabd7e8f09daa5851e92739bf68097` |
| `deliverables/ClickHouse十年历史表设计/01_schema.sql` | 10226 | `673f075029d37c3b2fa8996436ea5786358477cbceb053db3985bf6f33ef6921` |
| `deliverables/ClickHouse十年历史表设计/02_cluster.sql` | 10811 | `a78755f5ee1f1f1fffef821e8ad61a9e787c23e76f284b9fe74419b7e8a0784e` |
| `deliverables/ClickHouse十年历史表设计/03_queries.sql` | 9536 | `89894d7274a8ff2f8e2ec1a803788627f6dd3b6b6f30e585b7c425ae6c01d0ed` |
| `deliverables/ClickHouse十年历史表设计/04_optional_projection.sql` | 660 | `f9a2ef37b5b99e4f676f7c3a35536a4844d1cadf9ed2b05e21c405adbb6d88c8` |
| `deliverables/ClickHouse十年历史表设计/05_quality_checks.sql` | 2875 | `6314c98a19aa5efd6e2b8980b430bb47a006de3cde7253c7fe90392e65f203e7` |

<a id="gateway"></a>
## 实时网关

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `docs/realtime-gateway-contract.md` | 6225 | `2ddd884d71e94e30347d6591062fda24faf969575f5425a16249a1eacece731d` |

<a id="quality"></a>
## 数据质量边界

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `docs/second-level-backtest-upgrade.md` | 21586 | `744821c4950adb2f542a8f3ba07d8c80adf527d60b978de8c5ccb1e24e1f8283` |
| `docs/depth-snapshot-validation.md` | 1682 | `874026b88df7b8542d35e14ceb3d1fab102ed2a46496cb78b88eec6eb663a3b6` |
| `docs/closing-price-integrity.md` | 6315 | `c38aafd62f47a6fd2b03e4f53afcee813f9f511cd3f987f90dd90f979bbdad28` |

<a id="execution"></a>
## 研究与模拟账务

| 原仓库路径 | 字节数 | SHA-256 |
| --- | --- | --- |
| `docs/strategy.md` | 23939 | `e78dc1ebf0489418fad5eac6d780ec109fd904cb81c9894ceefd7db61461b916` |
| `docs/paper-trading-plan.md` | 25672 | `0503d9f24bf15a8768e10ff5450093d8ecd8f06a5e337646da5d36b48fd9831b` |
| `docs/corporate-actions.md` | 6354 | `f5057a2a1cf16ad88b9bd01450e3237e43989c050a6a2f524e9d092e03ceafff` |

## 用户参考图

原图尺寸：6331 × 5070；交付副本：`assets/reference-TSP.png`。

SHA-256：`085351c4a447c674ef77cda3b903bd33a441b2f3bb22c720ea0d40b4475fe64e`。
