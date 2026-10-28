# 分钟采集最终门禁记录

日期：2026-10-29。基线：v0.4.37 / f4d8c11b。
证据以[核验说明](verification.md)和[两端基准JSON](benchmarks.json)为准。
本轮工作区未提交；Linux候选用于隔离验收，常驻服务未升级。

## 实际执行

全部pytest使用独立临时数据目录、`TICKFLOW_ENV_FILE=/dev/null`、
`SERVICE_ROLE=standalone`、`MULTITENANCY_ENABLED=false`和有界Polars线程；
不读取真实行情、身份或SMTP运行配置。真实基准仅提取各端网络参数。

| 命令/验证 | 结果 |
| --- | --- |
| `uv run --frozen pytest tests -q`（最终全量） | 4,650 passed，162 skipped，173 warnings，172.98秒，exit 0 |
| 原生/安装/Provider/行情96项回归 | 96 passed |
| 候选镜像内 `python -m pytest test_rustdx_native_pool.py -q` | 14 passed，30.73秒 |
| `cargo fmt -- --check` | exit 0 |
| `cargo test --locked` | 7 passed，0 failed |
| `cargo clippy --locked --all-targets -- -D warnings` | exit 0 |
| `PYO3_CROSS=1 PYO3_CROSS_PYTHON_VERSION=3.11 PYO3_CROSS_PYTHON_IMPLEMENTATION=CPython cargo check --locked --target x86_64-pc-windows-gnu` | exit 0 |
| `uv run --project backend --frozen --no-sync pytest tests/tooling -q` | 136 passed，50.07秒 |
| `python3 scripts/check_ruff_delta.py --base HEAD --ruff <环境内ruff>` | 17文件，42项既有，0新增，exit 0 |
| `python build_manifest.py verify --root /app --manifest /app/audit-build-manifest.json` | changed/missing/added均为[]，exit 0 |
| 同名单两端full/missing取数及独立Parquet逐键核对 | exit 0；两端候选均504,000唯一键，无剩余缺口 |
| `make check` | 87路径、31文档、8需求模块、v0.4.37及diff检查通过，exit 0 |

实际原生TCP测试使用应用虚拟环境Python；临时pytest仅安装到测试目录，
没有修改常驻环境依赖。桥接限定8业务连接、1探测连接，
两端所有基准的实际峰值分别为8和1。
全量测试按2026-10-29真实运行日期执行；仅“未来日”校验用例固定自己的业务时钟。
跳过项和既有warnings不算通过，Windows仅编译检查，未执行Windows原生套接字测试。
固定核心文档测试另有18项既有ignored，不计入55项通过。
报告、六类文档入口和JSON证据写回后已执行根`make check`。

## 证据身份

| 工作区文件 | SHA256 |
| --- | --- |
| `backend/native/rustdx_native/src/lib.rs` | `13768cb3baf34670e2da769ecd67cd1538b19f5e64bd765b99af811346732173` |
| `backend/tests/test_rustdx_native_pool.py` | `e807b87c5e5075c208c24dd06395aaa255e8d5425d3a8735181c65161f345ced` |
| `backend/app/plugins/rustdx/client.py` | `abaf05c4e0d6a47b4a5ac5c1997fd4163d92f073ea0edca49be1d88eb4075042` |
| `backend/app/plugins/rustdx/provider.py` | `98d263f5baf488a3e7088467527e19b5ae6e996bbefcb7943e89a531e94a3265` |
| `backend/app/services/kline_gaps.py` | `928bd506653c348a0cb9c7e466bdddb8a958f7f2b8a089f2a4b9ecf158e0f7b1` |
| `backend/app/services/kline_periods.py` | `0e51ae068a5629ca023aa90f8dabdda2d08372a336c8b19635628ffba4b4c6f8` |
| `backend/app/api/kline.py` | `bc76f8434b66687bfe43dc65e2484aa4ccdefb6af3b95fc8d4693b81b15b55ac` |
| `backend/app/services/repair_daily.py` | `4ae70e59e8e25ff26821e834a307c2d8b1dbf276dfb62162d8a009d0e8b8ecec` |

修改范围为客户端、Provider、共享周期/缺口服务和原生预算；
API与修正服务的warning接线来自同期用户追加逐证券隔离要求。
前端代码、依赖版本和应用版本没有变化，不用后端结果替代前端构建或正式发布门禁。
