# 扩展概念与行业默认拉取修复验收

验收日期：2026-10-17（北京时间）。应用版本：0.4.2；本次为功能分支修复。

## 问题与修复

- `386d404` 删除了两项默认 URL，改为环境变量必配；空环境因此生成 URL 为空、
  自动拉取关闭的预置表。
- 恢复概念与行业默认接口；环境变量未设置或为空均保留默认值，非空值独立覆盖。
- 启动时仅修复没有 Parquet 数据、字段及拉取配方未经定制的旧空模板；保留自定义
  URL、主动关闭状态、调度、鉴权、已有数据及创建时间。
- 手动预置拉取复用通用拉取服务，遵循保存的 URL、请求头、鉴权、字段映射和北京日期。
  保留预置接口数组及常见 JSON 信封兼容。
- 完整多租户的管理员配置列表此前读取隐藏 `pull` 的公共副本，导致页面仍显示空 URL。
  现由具备 `settings:manage` 的管理员会话转发至平台；普通账户仍读取公共副本，
  `read:ext` Token 不可访问管理配置列表。

## 本机真实接口验收

本机平台服务与页面 API 已加载修复。通过真实管理员会话请求页面使用的接口：

| 项目 | 扩展概念 `ext_gn_ths` | 扩展行业 `ext_hy_ths` |
| --- | --- | --- |
| 配置列表 | HTTP 200，默认 URL 可见 | HTTP 200，默认 URL 可见 |
| 自动拉取 | 启用，每分钟 | 启用，每分钟 |
| 手动 `/pull/run` | HTTP 200，5,228 行 | HTTP 200，5,228 行 |
| `/rows?limit=1` | HTTP 200，总计 5,228 行 | HTTP 200，总计 5,228 行 |
| 维度字段 | `所属概念` | `所属同花顺行业` |

行业 `/presets/ext_hy_ths/fetch` 另行验证为 HTTP 200、5,228 行。
两表继续通过公共数据版本发布；检查公共配置文件确认不含 `pull`。
非交易时段调度会记录 `skipped / 不在拉取时间窗口内`，这表示正常门控，
不影响已完成的手动拉取和数据读取。

本次未执行浏览器自动化：会话未提供可调用的浏览器工具。
页面验证依据是前端组件实际使用的配置列表、手动拉取和数据读取接口。

## 回归验证

扩展数据相关测试：**93 passed**。

```bash
python -m pytest \
  tests/test_ext_presets_startup.py tests/test_ext_pull_time_window.py \
  tests/test_ext_pull_refresh.py tests/test_ext_pull_auth.py \
  tests/test_ext_preset_dimension_values.py tests/test_ext_factors.py \
  tests/test_ext_pull_pagination.py tests/test_ext_pull_timeout_config.py -q
```

平台与账户测试：使用真实 PostgreSQL 的一次性数据库，**46 passed**。

```bash
TSP_TEST_POSTGRES_DSN='<一次性 PostgreSQL 根 DSN>' python -m pytest \
  tests/test_full_administration.py tests/test_full_identity.py \
  tests/test_full_platform.py -q
```

新增回归覆盖管理员读取完整配方、普通账户不转发、不泄漏上游请求头、
管理员所属 `read:ext` Token 不提升为配置管理凭据，以及转发保留查询参数、
内部认证且不携带浏览器 Cookie 或租户请求头。
修复前管理员配置读取与平台路径白名单测试失败，修复后通过。

本次修改的预置服务、权限转发及对应测试通过 Ruff；
`config.py` 和 `ext_pull.py` 分别保留 3、13 项既有诊断，比较 HEAD 无新增诊断。
`git diff --check` 与应用版本事实源一致性校验通过。
前端源码、原生桥接及全市场首次同步实现未改动。
