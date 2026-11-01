# TSP 的 TRAE 已删除提示词清理记录

核查日期：2026-10-30。范围：本机 TRAE CN 中当前项目 TSP 及其原名 TSPP 的历史输入、日志残留和相关本地备份。

**状态：已清理 1 条确认的日志提示词残留；历史输入及加密归档的选择性清理尚未完成。** 本记录不包含提示词正文、凭据或数据库副本。

## 已实际执行的清理

在当前日志的消息元数据中，找到了同时具有项目路径、用户消息 ID 和原始输入的记录。该消息 ID 也存在于 `message_deleted` 事件中，删除事件报告 `archived_count=2`。

| 项目 | 核对值 |
| --- | --- |
| 会话 ID | `<REDACTED_ID>` |
| 已删除的用户消息 ID | `<REDACTED_ID>` |
| 处理文件 | `~/Library/Application Support/Trae CN/logs/20261030T215254/window1/renderer.log` |
| 已清除字段 | `user_message_context.query`、`user_message_context.parsed_query` |
| 处理数量 | 1 条消息、1 条日志记录 |
| 验证 | 原始输入字段为空，解析输入列表为空；消息 ID 和删除事件仍保留 |

只在原记录的字节区间内写入清理后的合法 JSON，并保持记录长度及后续日志偏移不变。写入前核对原记录和文件 inode，写入后复读验证。未删除日志文件或目录，未保存包含原始提示词的清理备份。

## 历史输入主库与备份

工作区归属由各自的 `workspace.json` 确认。下列相对路径以 `~/Library/Application Support/Trae CN/User/workspaceStorage/` 为根。

| 工作区 | 主库 | 主库输入条数 | `.backup` 输入条数 | 本次删除历史条数 |
| --- | --- | ---: | ---: | ---: |
| 原项目 TSPP | `<REDACTED_ID>/state.vscdb` | 100 | 100 | 0 |
| 当前项目 TSP | `<REDACTED_ID>/state.vscdb` | 46 | 42 | 0 |

查询键为 `icube-ai-agent-storage-input-history`。条目包含 `inputText`、`parsedQuery`、`multiMedia` 和 `files`，没有消息 ID、会话 ID、时间戳或删除标记。

可核对正文的上述已删除消息，与四份输入历史中的解析输入或完整文本均未匹配。因此没有删除历史条目。该结果仅适用于这一条有正文对应关系的消息，**不能证明其他已删除提示词没有残留**。

四份历史库的 `PRAGMA quick_check` 均返回 `ok`；清理前后逐项比较输入历史，内容保持相同。未改写主库或备份，未清空整个历史列表。

与[昨天的核查](trae-prompt-history-audit-20261029.md)相比，旧 TSPP 的备份现在包含 100 条输入，昨天记录为 0 条。说明备份状态发生了更新，不能继续沿用旧报告中的备份存量。

## 其他本地存储核查

| 位置 | 本次核查结果 | 处理 |
| --- | --- | --- |
| `User/globalStorage/state.vscdb` 及 `.backup` | 没有上述输入历史键；两份库 `quick_check=ok` | 保留其他全局状态 |
| `Backups` | 没有文件 | 无需清理 |
| `~/.trae-cn/memory/projects` | 未发现 TSP 或 TSPP 专属项目记忆目录 | 无已识别副本 |
| `~/.trae-cn/worktrees/TSP`、`TSPP` | 两个目录均无文件 | 无需清理 |
| 桌面、下载、文稿及回收站中可扫描的导出位置 | 未发现独立的 TRAE 数据库或已识别的聊天导出副本 | 未删除项目文件 |
| 其他本机 TRAE 安装数据根 | `Application Support` 和用户主目录中只发现本次使用的 TRAE CN 数据根 | 未发现另一套安装数据根 |
| `ModularData/ai-agent` | 存在主库 `database.db`、WAL 和共享内存文件，未发现另一份同目录数据库备份 | 未改写 |
| `ModularData/ai-agent/snapshot` | 属于问答的 Git 文件快照；未确认是已删除提示词正文副本 | 保留文件恢复状态 |

另外扫描了 14,305 个缓存文件，按上述已删除用户消息 ID 进行明文检索，未命中：

| 类别 | 已扫描文件数 |
| --- | ---: |
| Local Storage | 9 |
| Session Storage | 9 |
| IndexedDB | 6 |
| Cache | 704 |
| Code Cache | 4 |
| CachedData | 1,285 |
| WebStorage | 132 |
| Partitions | 12,124 |
| Service Worker | 22 |
| conversation-tips | 10 |

明文 ID 检索未命中不排除压缩、加密或不携带消息 ID 的副本。此轮扫描不包含云端或系统级备份。

## 尚未完成的部分

1. **早期已删除提示词与历史输入的对应关系。** 核查开始时可读取多组旧日志，之后旧日志目录在核查期间消失，最终只剩当前会话的 4 个 renderer 日志文件；本任务未执行日志目录删除，消失原因未确认。昨天报告记录了 161 个删除事件，但没有正文映射，不能据此筛选当前的历史输入。
2. **加密核心库中的归档。** `ModularData/ai-agent/database.db` 是加密存储。安装包的实现包含归档标记和消息删除逻辑，但没有找到可独立调用、读取归档正文并按已删除消息永久清理的维护接口。本次没有读取、改写、解密导出或删除该库及 WAL，不能宣称归档已清除。
3. **继续清理所需数据。** 历史输入的精确清理需要已删除提示词的可辨识文本，或含消息 ID 与正文对应关系的导出。加密归档仍需要 TRAE 提供选择性永久清理能力；单纯再次执行“删除消息”或清空全部快捷输入历史不能证明对应归档已永久删除。

本次没有修改应用代码、版本、运行配置或已有工作区变更，没有提交或推送。
