# TSPP 的 TRAE 提示词历史核查

核查日期：2026-10-29。范围：本机 TRAE CN 中属于 `/path/to/home/Documents/TSPP` 的提示词历史及相关备份。

**状态：完成可读取存储的核查，尚未执行删除。** 删除事件与现存快捷输入历史之间缺少可验证的对应关系；核心对话库为加密存储，当前工具不能读取归档正文或执行选择性永久清理。本报告不包含提示词正文、授权码或数据库副本。

## 已确认的项目归属与记录

工作区标识为 `<REDACTED_ID>`，对应 `workspace.json` 中的 `file:///path/to/home/Documents/TSPP`。TRAE 当前界面也显示项目 TSPP。

工作区状态中的会话配置键确认了下列会话归属。对日志里的 `[Realtime] event: message_deleted` 按消息 ID 去重后，得到 161 条删除记录：

| 会话 ID | 删除消息数 |
| --- | ---: |
| `已脱敏会话1` | 49 |
| `已脱敏会话2` | 68 |
| `已脱敏会话3` | 1 |
| `已脱敏会话4` | 12 |
| `已脱敏会话5` | 0 |
| `已脱敏会话6` | 28 |
| `<REDACTED_ID>` | 3 |
| 合计 | 161 |

删除事件包含 `chat_session_id`、`message_id`、`deleted_at` 和 `archived_count`。这些事件的 `archived_count` 均为 2，表明当时的删除处理报告了归档行为；这不证明相关正文目前仍在数据库，也不能当作当前归档存量。

## 存储与备份核查

下表中的相对位置以 `/path/to/home/Library/Application Support/Trae CN` 为根。

| 位置 | 实际读取结果 | 处理状态 |
| --- | --- | --- |
| `User/workspaceStorage/<REDACTED_ID>/state.vscdb` | `icube-ai-agent-storage-input-history` 有 100 条记录 | 保留，缺少与已删除消息的对应关系 |
| 同目录 `state.vscdb.backup` | 同一历史键为 `[]`，0 条可查询记录 | 未删除整个备份；它还保存编辑器等其他状态 |
| `User/globalStorage/state.vscdb` 及 `.backup` | 未发现同名输入历史键或删除归档键；TSPP 引用主要为打开路径、Git、Python 和终端配置 | 保留 |
| `ModularData/ai-agent/database.db` 及 WAL | 主库没有明文 SQLite 文件头；普通 SQLite 返回 `file is not a database`；原生模块包含 SQLCipher 支持 | 未读取或改写加密库 |
| `Backups` | 目录为空 | 无文件可处理 |
| `/path/to/home/.trae-cn/memory/projects` | 未发现 TSPP 专属项目记忆目录 | 无已识别的 TSPP 记忆副本可处理 |
| `/path/to/home/.trae-cn/worktrees/TSPP` | 目录为空 | 无文件可处理 |
| `ModularData/ai-agent/snapshot` | 存在按会话组织的 Git 文件快照 | 未认定为已删除提示词正文副本，保留 |
| `logs` | 可读取日志中存在消息删除事件和消息 ID | 未将删除事件元数据当作提示词正文删除 |
| `Local Storage`、`IndexedDB`、缓存目录、MCP 输出目录 | 核查了文件布局；部分样本消息 ID 的明文检索没有命中 | 不能据此排除压缩或加密副本，未改写 |

输入历史条目只有 `inputText`、`parsedQuery`、`multiMedia` 和 `files`，包括嵌套字段在内未找到消息 ID、会话 ID、时间戳或删除标记。因此，不能按日志里的 161 个已删除消息 ID 安全筛选现存 100 条输入历史。

工作区主库和工作区备份的 SQLite `quick_check` 均完整返回 `ok`。未执行写入、压缩、备份导出或恢复；未验证加密核心库完整性。

## 尚未完成的清理

1. 精确匹配已删除提示词与现存快捷输入历史，需要已删除提示词的可辨识文本，或 TRAE 提供的消息 ID 与正文对应数据。
2. 核查并永久清理核心库中的对应归档，需要 TRAE 支持的归档读取、选择性清理接口或维护工具；当前工具无法完成。
3. 若匹配到可查询的残留，继续只清理匹配项并复核对应备份。不能以清空全部 100 条输入历史替代用户要求的选择性清理。

本次仅新增这份核查报告；未改动项目应用代码、版本或运行配置，也未提交或推送。上述结果限于已核查的本机位置，未验证云端、系统备份或加密日志中的副本。
