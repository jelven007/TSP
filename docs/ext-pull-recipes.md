# 扩展数据拉取配方

扩展概念、扩展行业和自定义扩展表共用同一套 HTTP 拉取、标准化、
Parquet 写入及缓存失效流程。管理员可配置 URL、鉴权、响应路径、
字段映射、分页和定时执行；已有配置不会因升级而重置。

单级源已经返回标准化行时，应将“来源结构”设为“已标准化行”，避免套用
内置概念或行业来源的数组展开转换。分页只有在空页或短页出现时才视为完成；
到达 `max_pages` 仍无法确认终页会拒绝写入。日期检查覆盖每一行并在转换前执行。

## 两级配方

上方 URL、响应路径和分页配置用于读取目录。可再声明一份 JSON 配方，
针对每个目录项请求成员接口：

```json
{
  "member_url": "https://api.example.com/members",
  "id_field": "id",
  "member_param": "sector",
  "member_path": "data.rows",
  "inject_fields": {"name": "所属概念"},
  "group_by": "symbol",
  "join_fields": {"所属概念": ";"},
  "concurrency": 4,
  "min_interval_seconds": 0.1,
  "retries": 2,
  "backoff_seconds": 1,
  "timeout_seconds": 300
}
```

目录行例如 `{"id":"BK001","name":"人工智能"}`。成员请求会追加
`?sector=BK001`，参数值经过 URL 编码；目录内容不能指定请求主机。
成员响应中的每一行会注入声明的目录字段。字段映射完成后，
`join_fields` 按目录顺序合并同一 `group_by` 标的的多个分类值。

成员分页使用 `member_page_param`、`member_page_size_param`、
`member_page_size`、`member_page_start` 和 `member_max_pages`。
目录与成员仅支持同协议、同主机、同端口的 GET 请求，并共用请求头、
密钥和日期参数。系统不会执行表达式、脚本或目录提供的 URL。

并发范围为 1 至 8，目录最多 5,000 项，整轮最多 200,000 行，
整轮时限最多 900 秒。网络错误、429 和 5xx 可按配置补拉；
已完成成员不会重复请求。`Retry-After` 作用于共享请求预算，
4xx 非限流错误不重试。任何成员失败都会保留当前快照，不发布部分结果。

## 完整快照

`snapshot_policy=auto` 对内置概念、行业及两级配方执行完整替换；
普通自定义来源沿用合并语义。也可显式选择 `replace` 或 `merge`。
完整替换前检查声明字段和有效数据，上一份文件保存为
`ext_data/<id>/previous-complete.parquet.bak`，只保留一份且不参与查询视图。

HTTP 错误、成员失败、日期不符、分页截断、字段缺失或标准化后空数据均不会
覆盖当前快照。上传和 `/ingest` 保持原有合并语义。

- `last_attempt`：本轮目录或请求完成度及失败清单，不代表全市场股票覆盖率。
- `last_complete`：最后成功的接收时间、源端日期、脱敏来源、行数、文件哈希和本地股票池覆盖。
- `previous_complete`：仅当被替换文件仍匹配上一成功记录哈希时保留。

接收时间不能替代源端日期。缺少完整源日期或本地标的维表时，相关状态明确为未知。

同一数据源在同一进程内只允许一轮拉取，配置保存、上传、删除和快照发布互斥；
请求期间更换来源、字段或标的映射会拒绝旧结果。部署仍必须维持公共数据单写者，
不能让多个独立采集进程同时写同一目录。

恢复旧快照前应停止该源调度、备份当前文件，并核对
`previous_complete.sha256`。维护者将 `.bak` 恢复为 `part.parquet` 后，
需要重启数据服务或显式失效缓存。界面不会自动回滚新结果。
