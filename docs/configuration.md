# 配置详解

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页面可视化修改。本文件解释每个配置项的作用。

部署相关配置(端口/密码/老 CPU 兼容)的实操见 [deployment.md](./deployment.md)。

完整单机多租户通过 `MULTITENANCY_ENABLED`、`SERVICE_ROLE` 和
`TSP_CELL_SCHEMA_VERSION=6` 接线，使用独立控制库/业务库 runtime DSN。
`TSP_CELL_*` 名称为当前共享基础的兼容配置，仍在使用。
平台配置、公共版本、Broker 和各服务环境文件见
[完整部署配置](../deploy/full/README.md)。本页其余初始化说明适用于独立运行模式。

---

## 数据源

```ini
TICKFLOW_API_KEY=              # 留空 = None 模式(历史日K免费);填 Key = 按订阅档位解锁
```

项目首次启动时七项数据能力默认路由到 `rustdx`;旧源路由自动恢复为默认值，同时支持切换到 TickFlow、
fuyao 或自定义数据源(YAML 声明自有接口见
[custom-data-source.md](./custom-data-source.md),插件开发见
[plugin-development.md](./plugin-development.md))。

仓库内置的 `rustdx` 插件无需 API Key，可提供日K、除权因子、实时行情、
分钟K、五档盘口、财务和全量分钟七项能力。Docker 镜像和桌面包已包含原生扩展；
源码环境具备 Rust 编译工具后，执行 `uv sync --frozen` 即默认安装；
启动和构建时校验桥接版本与接口。公开通达信服务器可能限流或不可达，安装、
环境变量、单位校准、竞价采集及覆盖边界见
[rustdx 数据源](./rustdx-data-source.md)。
七类能力共用最多 35 条持久连接，历史财务也通过 rustdx 下载并独立解析。
选择任一非 TickFlow 源后，请求失败不会静默切回 TickFlow。

- **留空(None 模式)**:通过 free-api 使用历史日 K(当日数据盘后 1-2 小时可用),**无需付费**即可体验核心选股/回测功能
- **填入 API Key**:按你的订阅档位解锁更多能力

### TickFlow 备选源按档位提供的实时能力

| 档位     | 实时能力                                 |
| :------- | :--------------------------------------- |
| Free     | 自选页前 5 个标的实时监控(最低 6 秒刷新) |
| Starter+ | 全市场实时行情                           |
| Pro      | 分钟 K + 盘口                            |
| Expert   | WebSocket + 财务数据 + 全量分钟          |

> 完整能力矩阵见 [tickflow.org/pricing](https://tickflow.org/pricing/),高等档位含较低档全部权益。
> 在面板 **设置 → 凭据与能力** 点「重新检测」可查看当前档位标签。
>
> **档位仅适用于 TickFlow 数据源**。功能门槛的统一标准是"能力"(`kline.minute.batch`、`depth5.batch`、`financial` 等能力键):其他第三方/自定义数据源以声明的数据集能力为准,系统会按当前数据源配置自动合并判定,UI 提示一律以能力名表达,不再依赖 TickFlow 档位名。

### 全量分钟 (full_minute)

「全量分钟」是一项**独立能力**(能力键 `full_minute`,探测名 `intraday.universe`),与其他能力同样**可路由**:盘中把全市场当日 1 分钟 K 持续增量落盘到本地 `data/kline_minute/` 当日分区,分钟策略(`minute_filter`)与分时视图即可读到新鲜数据。接入方式二选一:

- **TickFlow Expert**:配置 Expert 档 Key,零配置即用(修复轮 `intraday.batch` + 稳态 `intraday.universe` 单请求增量)
- **插件/自定义源**:声明 `full_minute` 数据集并在 **设置 → 数据源 → 全量分钟** 路由到该源 — Python 插件实现 `get_intraday_batch`(必需)/`get_intraday_latest`(可选,未实现自动降级仅修复轮、节奏下限 60s);YAML 声明式源数据集配置与 `minute` 同形(仅修复轮语义)。契约细节见 [plugin-development.md](./plugin-development.md) 与 [custom-data-source.md](./custom-data-source.md)

接入步骤:

1. **设置 → 凭据与能力**(TickFlow 路径)配置 API Key(Expert 档),或在 **设置 → 数据源** 声明/安装提供 `full_minute` 的源并路由;点「重新检测」后能力列表出现「全量分钟」
2. 落盘服务默认开启；仅在交易日 09:15–11:30 / 13:00–15:15 运行，午休和 15:15 后自动暂停
3. 冷启动(如 10 点才开服务)自动触发**全天修复轮**,一次批量补齐当日缺口;稳态走**增量轮**(偏好可配 1–120 秒)。rustdx 逐证券取 bars，有效周期至少 60 秒；设置页显示生效周期，原偏好保留
4. 与盘后分钟同步写同一分区，完整读/合并/替换加锁，`unique(symbol, datetime)` 幂等合并

说明:标的池为 A 股股票(CN_Equity_A),ETF 不在内(分时走批量补拉路径);覆盖滞后超阈值或连续空轮会自动再跑修复轮自愈。

---

## AI(可选)

用于自然语言生成策略。**所有配置留空即跳过**,不影响核心功能。支持任意 OpenAI 兼容接口。

```ini
AI_PROVIDER=openai_compat              # openai_compat | openai | codex_cli
AI_PROTOCOL=chat_completions           # chat_completions | responses
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_DAILY_TOKEN_BUDGET=500000           # 每日 token 预算上限
```

| 配置项 | 说明 |
| :--- | :--- |
| `AI_PROVIDER` | `openai_compat`（兼容服务）、`openai`（支持推理强度配置）、`codex_cli`（本机 CLI） |
| `AI_PROTOCOL` | 默认 `chat_completions`；服务商支持时显式选择 `responses`。Codex CLI 不使用此项 |
| `AI_BASE_URL` | 接口地址,如 DeepSeek `https://api.deepseek.com/v1` |
| `AI_PROTOCOL` | 显式选择 `chat_completions`（默认）或 `responses`；设置页保存值优先，不自动切换协议 |
| `AI_API_KEY` | 留空则关闭 AI 功能 |
| `AI_MODEL` | 模型名,如 `deepseek-chat` |
| `AI_DAILY_TOKEN_BUDGET` | 每日 token 预算,超限后当日不再调用 |

页面预设包括自定义、OpenAI、DeepSeek、GLM 和 Codex CLI。
通义千问与 Kimi 不再单列预设；已有其他 OpenAI 兼容配置会显示为“自定义”，
其地址、模型和密钥仍按原配置使用。

「接口协议」用于报告、策略生成及助手。页面保存的协议优先于环境变量；
旧客户端省略协议字段时保留已保存值，清空 AI 配置后恢复 Chat Completions。
现有「测试」按钮先保存表单再测试，选择协议本身不触发请求。
Responses 的工具、完整性及取消约定见 [AI 协议](./ai-protocols.md)。

接入示例见 [strategy.md](./strategy.md) 的「AI 生成策略」章节。

---

## 内置扩展概念与行业

```ini
EXT_CONCEPT_DATA_URL=
EXT_INDUSTRY_DATA_URL=
```

扩展概念和扩展行业默认可直接拉取同花顺分类数据，默认接口分别为：

- `https://shy313.com/api/plugins/market_flow/exports/ths-concepts`
- `https://shy313.com/api/plugins/market_flow/exports/ths-industries`

环境变量未设置或为空时使用上述默认值，非空 URL 可独立覆盖对应上游。

- 新安装默认在交易日 `09:15-11:30`、`13:00-15:15`
  每分钟拉取一次。
- 调度器启动后会立即执行首轮，之后受交易日和双时段门控。
- 自动拉取可在「数据 → 扩展概念/行业 → 拉取设置」关闭。
- 旧版原样生成、没有数据的空预设会恢复默认地址和启用状态；已保存的自定义地址、
  主动关闭状态、定制配方及已有数据保持不变。
- 手动拉取使用表内保存的 URL、请求头、鉴权和字段映射，与定时拉取一致。

完整多租户模式下，管理员的配置列表从平台服务读取，显示保存的拉取地址和调度；
普通账户仍读取公共数据副本，不返回拉取配方或上游请求头。

预期概念响应行为 `{symbol, name, concepts: [...]}`，行业响应行为
`{symbol, name, industries: [...]}`；接口可直接返回数组，也可使用常见
`data`、`list`、`rows` 等信封字段。

---

## 服务

```ini
HOST=0.0.0.0          # 开发服务监听地址 / Docker 主机绑定地址
PORT=3018             # 开发后端端口 / Docker 主机映射端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
```

- `HOST`:`0.0.0.0` 监听所有网卡(容器/公网部署需要);仅本机用可设 `127.0.0.1`
- `PORT`:默认 `3018`;开发模式兼容显式的 `BACKEND_PORT` 覆盖,改端口后 SSH 转发命令也要同步改
- `LOG_LEVEL`:排查问题时改 `DEBUG`

---

## 数据

```ini
DATA_DIR=./data       # Parquet / DuckDB 数据存储目录
```

整个 `data/` 目录都不纳入 git —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据。

如需迁移数据,直接拷贝整个 `data/` 目录即可。详见 [deployment.md → 更新代码](./deployment.md#更新代码已部署用户必读)。

### 数据页初始化默认值

新项目按以下参数初始化「数据」菜单；后端偏好读取、页面初始显示及设置中的
“默认”预设保持一致。已保存的值优先，包括主动关闭状态；缺失字段才使用默认值。
完整多租户模式下，数据页参数由平台服务统一维护。

| 配置 | 初始化默认值 |
| --- | --- |
| 日K、复权、分钟、全量分钟、五档、实时、财务数据源 | 全部 `rustdx` |
| 实时行情 | 开启，轮询偏好 1 秒 |
| 证券目录同步 | 交易日 09:10 |
| 盘后管道 | 交易日 15:35；A 股、指数开启，ETF 关闭 |
| 指标计算 / 指数日K批量大小 | 1000 / 100 |
| 分钟K自动同步 | 开启，最近 5 天，分段大小 20 个交易日 |
| 盘中全量分钟刷新 | 开启，间隔偏好 1 秒；rustdx 生效周期至少 60 秒 |
| 市场环境盘后自动计算 | 开启 |
| 市场环境全量回填 | 每批 120 个交易日，预热 40 个日历日 |
| 扩展概念、扩展行业 | 快照模式，内置默认 URL，GET，无需鉴权；自动拉取开启，间隔 1 分钟、超时 30 秒 |
| 数据画像卡片 | 默认顺序为证券目录、日K、复权、指标、指数、ETF、分钟K、财务、市场环境；ETF 隐藏，其余按数据能力显示 |

扩展数据的交易时段、首次自动拉取及自定义 URL 规则见上文
[内置扩展概念与行业](#内置扩展概念与行业)。

### 异动监控初始化默认值

竞价异动、盘中异动、偏移异动默认均启用，进入对应页面后自动加载。
竞价历史从首次成功采集起积累；尚无交易日快照时显示空状态。
偏移异动在交易时段每 60 秒计算一次，非交易时段每 5 分钟刷新。
偏移开关按当前账户保存在浏览器中，个股详情使用同一开关；主动关闭后停止计算，
仍可查看上次结果，重新打开页面也保留关闭状态。
后台告警由「监控中心」的规则独立控制。

---

## 邮箱账户(公网部署)

```ini
AUTH_EMAIL='admin@example.com'
AUTH_PASSWORD='你的密码'  # 至少 8 位; 仅首次生效

# 浏览器注册口令与验证码 SMTP
AUTH_REGISTRATION_SECRET='请设置注册口令'  # 仅用于首次生成哈希
AUTH_SMTP_HOST='smtp.example.com'
AUTH_SMTP_PORT=465
AUTH_SMTP_SECURITY='ssl'  # ssl | starttls | none
AUTH_SMTP_USERNAME='no-reply@example.com'
AUTH_SMTP_PASSWORD='SMTP 密码或授权码'
AUTH_SMTP_FROM_ADDRESS='no-reply@example.com'
```

`AUTH_EMAIL` 与 `AUTH_PASSWORD` 同时设置时, 服务首次启动会预置邮箱账户。预置账户是部署初始化, 不要求验证码。已有账户后环境变量不再覆盖页面中维护的密码。

浏览器注册会先校验注册口令, 正确后才发送邮件验证码。`AUTH_REGISTRATION_SECRET` 首次启动后会以带随机盐的 PBKDF2 哈希保存到 `auth.json`, 确认成功后可从 `.env` 删除明文并重启。全新部署还需配置 `AUTH_SMTP_*`; `AUTH_SMTP_HOST` 留空时会尝试复用历史已保存的系统邮件 SMTP。验证码 10 分钟有效、60 秒后可重发, 连续输错 5 次后失效。

“设置 → 实时监控 → 邮件”始终复用上述注册 SMTP，仅允许修改收件人。主机、端口、加密方式、登录用户名、发件人和密码/授权码均由系统维护，页面与保存接口不允许修改；授权码仅在后端使用，保存收件人不会复制或改写凭据。收件人默认显示当前账户的注册邮箱，测试邮件使用该默认值；共享后台通知默认发给已注册账户，保存自定义收件人后以保存值为准。发信服务和注册账户均可用时，新建监控规则默认选择邮件；已保存的默认渠道（包括关闭）保持不变。未配置 `AUTH_SMTP_HOST` 时保留历史系统 SMTP 作为兜底，桌面版需由系统配置发信服务并填写收件人。

只配置 `AUTH_PASSWORD` 时保留旧版兼容模式, 首次访问需使用原密码绑定邮箱。密码建议使用单引号包裹; `.env` 文件权限应保持 `600`。

详细步骤和旧版迁移方法见[邮箱账户初始化](./deploy-password.md)。

---

## 后端依赖 Extras(可选)

```ini
BACKEND_EXTRAS=             # 留空默认;legacy-cpu 兼容老 CPU
```

老 CPU 无 AVX2/FMA 支持时设为 `legacy-cpu`,会给 Polars 切到 `rtcompat` 运行时;需回测则 `legacy-cpu backtest`。Docker 构建和 `./dev.sh` / `.\dev.ps1` 都会读取此值并同步依赖。详见 [deployment.md → 老 CPU 兼容](./deployment.md#老-cpu-兼容avx2fma-缺失)。
PostgreSQL 客户端由单机和 full-product 共用的模拟盘/告警模块直接引用，已属于默认
运行依赖；旧 `--extra cell` 命令仍兼容，但不再决定主应用能否导入。

---

## 配置优先级

1. **面板设置页**(`设置 → ...`):UI 修改后立即生效,持久化到 `data/`
2. **`.env` 文件**:启动时读取
3. **环境变量**:Docker / 系统环境变量,优先级最高

> 多数配置可在面板设置页修改,无需手动编辑 `.env`。仅 AI Key、API Key 等敏感项建议放 `.env`(不提交到 git)。
