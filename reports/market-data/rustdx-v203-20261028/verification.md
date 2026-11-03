# rustdx v2.0.3 接入融合验证

日期：2026-10-28。应用版本保持0.4.36。
起始项目提交：`1c630cc2d13cf3e2ed545b9a4d9ff5b340d224af`。
上游：`jelven007/rustdx` v2.0.3，
`d6bf22a0e5954c778ef6ad58be8e5f81aacc75c3`。

## 实施范围

- 固定完整100个核心Rust源码文件及MIT许可证，使用本地精确2.0.3依赖。
  上游原始和适配后摘要分别登记；build.rs及离线工具拒绝未登记源码漂移。
- 桥接0.2.0/API3，单进程8业务+1探测，总上限9；行情、分钟、财务共用业务池。
  后台默认7，旧35配置被钳制到8。可选共享额度目录支持多进程；
  各进程业务容量之和须不超过8，额度目录变更明确要求重启。
- 行情以044e证券数量探测，财务以实际0004心跳应答探测；三个完整合格轮次才发布节点。
  失败时先释放旧TCP。缺精度、无效输入/数据、缺能力和取消明确失败。
- 池内共享QuoteMetadataStore并串行按所需市场预热，ETF使用上游证券精度。
  运行配置变化关闭旧池并新建元数据空间。
- 历史分页绑定实际节点、进程唯一连接身份与北京时间观察日，变化拒绝整个结果。
  最新页、历史结束边界定位、单次页复用和最大页数保护继续保留。
- 排队、重试继承绝对截止时间。单文件财务下载使用核心300秒任务预算，
  每个分块仍使用TCP请求超时；大文件不按块刷新总时限。
- 现有七项Provider路由、旧财务legacy字段、标准单位、日期和本地派生周期保持兼容。
  没有导入上游CLI/runner/独立指标业务，也未据核心升级自动增加Provider能力。
- 源码身份校验、构建缓存、构建清单与CI适用核心测试已接线，
  需求/架构/二开/用户/测试/运营/运维和脚本入口同步。

## 实际验证

最终177项rustdx Python回归、171项安装/构建清单及仓库工具测试通过；202项核心单元测试、
57项文档测试（另18项网络/本地文件示例按上游标注忽略）。
桥接7项Rust测试通过。新回归先复现旧池复用、排队超时、分页来源混合和大文件预算错误。
macOS arm64 wheel及sdist构建通过；从独立解压目录离线构建wheel通过，
仅在独立副本注入源码漂移后构建明确失败。桥接严格Clippy、已改文件rustfmt、
增量Ruff（无新增告警）、根make check的目录/文档/版本/diff门禁全部通过。
应用版本仍为0.4.36。

最终同步147个已复审文件前逐项核对共享工作区与起始提交内容；现有并行报告保留。
项目目录安装桥接0.2.0后，离线检查核对v2.0.3、固定提交、API3、核心/补丁摘要与8+1预算通过。
同步后的Python综合回归334项全部通过（66.62秒），桥接Rust测试7项通过，
根make check、源码身份及增量Ruff再次通过。
已安装模块身份见[installation.json](installation.json)。本轮范围未发现未解决的阻断问题。

桥接严格Clippy初轮发现Python参数数量和旧关闭写法，已局部调整。
固定上游核心在Rust1.99新增两类风格lint，共5处；保留原源码，
定向允许`manual_is_multiple_of`与`chunks_exact_to_as_chunks`后其余严格门禁通过。
这不等于声明上游原样严格Clippy零告警。

## 公开节点只读实测

行情站`117.34.114.13:7709`：
股票600519.SH、ETF588200.SH、指数000001.SH报价及日K/1分K、除权、legacy财务和F10读取成功。
5222只当前股票三轮行情覆盖均为100%，耗时544.7 / 548.2 / 498.6ms；
全市场报价、分钟和财务混合调用完成。业务峰值8、探测峰值1、无重试或失败。
三轮样本不足以认证正式P95，不声明相对旧版本提速。
详细结果见[live-smoke.json](live-smoke.json)。

财务站`120.76.152.87:7709`的目录148项；未来日期空预置包不能证明代表财务解析成功。
半年报首次整包验证触及60秒旧预算，已用本地慢分块回归复现并调整为300秒单文件预算；
修复后 `gpcw20260630.zip`（5,765,778字节）完整下载，大小和MD5通过；
000001、600519的每股收益、每股净资产、总/流通股本字段成功解析。
本轮34.2秒，节点三轮实际FinancialHeartbeat资格通过，证券数量为null；
无重试或失败。见[live-financial-complete.json](live-financial-complete.json)。

复现与验证命令（相对于项目根；源码环境先安装本地wheel）：

```bash
PYTHONPATH=backend python -m pytest backend/tests/test_rustdx*.py -q
PYTHONPATH=backend python -m pytest backend/tests/test_build_manifest.py backend/tests/test_rustdx_installation.py tests/tooling -q
PYO3_PYTHON="$(pwd)/backend/.venv/bin/python" cargo test --locked --manifest-path backend/native/rustdx_native/Cargo.toml
RUSTDX_SKIP_INTEGRATION_TESTS=1 cargo test --locked --manifest-path backend/native/rustdx_native/vendor/rustdx-core/Cargo.toml
cargo clippy --locked --all-targets --all-features --manifest-path backend/native/rustdx_native/Cargo.toml -- -D warnings
python scripts/vendor_rustdx.py
python scripts/check_ruff_delta.py --base HEAD --ruff "$(pwd)/backend/.venv/bin/ruff"
make check
```

## 交付边界

本次授权为源码引入与融合。未执行提交、推送、部署或服务重启。
既有管理员、SMTP、账号、行情分区与用户报告保留。
运行中的旧进程不会因磁盘替换自动加载新的原生模块，下一次受控启动须核对桥接身份。
macOS arm64执行本轮构建；Windows/Linux产物和生产流量尚未实际运行。
历史数据全量初始化、财务历史完整性与公司行为日期认证不由本次接入验证替代。
