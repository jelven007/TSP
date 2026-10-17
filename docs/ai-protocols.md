# AI 接口协议

在「设置 → AI → 接口协议」选择 Chat Completions 或 Responses，保存后生效。
初始值及旧配置仍使用 Chat Completions。协议独立于服务商预设，不根据模型名称、
URL 或错误猜测；不支持的端点、鉴权错误和断流均报错，不自动切换协议。
原有测试按钮先保存当前表单，再发起测试。

## 适配边界

`services/ai_provider.py` 保留 `generate_ai_text`、`generate_ai_text_with_tools`
及 `stream_ai_text` 的契约，Responses 共用 `services/ai_responses.py`。
助手保留既有 Chat/Gemini 流式适配；选择 Responses 时也调用同一 Responses
适配器。工具上层继续使用 `tool_calls`、`role:tool`、`tool_call_id`，
动作确认和每次工具执行前的权限复查不变。仅支持本地声明的 function 工具，
不开放供应商内置浏览器、终端或其他工具。

接口使用 `/responses`，`max_tokens` 映射成 `max_output_tokens`；
显式预算仍按配置封顶，报告调用传 `None` 时沿用服务商默认上限。
Responses 助手轮次使用配置的输出上限。OpenAI 的推理强度映射为
`reasoning.effort`；兼容服务不额外添加该参数。只有 400 明确拒绝 temperature
或 reasoning 时才逐项移除重试，每项至多一次；不删除工具、`store=false`
或输出预算。SDK 仍只对开始响应前的暂时性错误作原有有限重试。

## 工具往返及隔离

Responses 请求始终 `store=false`，不使用 `previous_response_id`。
请求 `reasoning.encrypted_content`，把响应原生 output（含加密推理项）仅保存在
当前生成任务的消息列表，以 `_responses_output` 随下一轮输入回传。
工具结果按原 `call_id` 转为 `function_call_output`，不重造关联 ID。
本字段不发送到浏览器、不写入会话历史，也不存在全局响应 ID 缓存。
既有 Chat 工具 schema 的可选参数保持可选，未显式启用 strict 时传 `strict=false`。

完整响应才授权本轮工具执行：`completed` 终态、完整且唯一的 call_id、
已声明的工具名、完整 JSON 对象参数必须同时满足。`incomplete`（包括长度和
过滤原因）、`failed`、缺失终态、空正文且无工具、拒绝内容均报错。
工具分片或 `output_item.done` 不单独授权执行，只有完整 `response.output`
是本轮工具清单。一次响应的全部工具先校验，通过后才逐个执行。
工具迭代期间切换为 Chat 协议时停止任务，重新发起才使用新协议。

## 流式与恢复

正文 delta 逐步显示，最终响应校验一致性；只有最终正文而没有 delta 时补发一次。
已显示部分正文后遇到截断也报错，不能作为完整报告或已完成工具轮次。
流开始后不重放请求；调用方取消、断开、停止消费或异常时关闭流和 HTTP 客户端。
用户重试会建立新任务；已执行的工具仍由各自既有去重/确认机制控制，
本适配器不承诺跨任务工具事务。

改动仅影响 AI 配置读取、secrets 文件缓存签名和前端 settings 查询。
不影响行情、指标、研究缓存、generation、公共发布或 SSE 行情协议。
回退时选择 Chat Completions 并保存即可；旧客户端保存其他配置不会清空协议。
环境变量协议非法会明确报错，应修正配置；不静默退回默认值。

验证使用真实 OpenAI SDK + 隔离 HTTP/SSE 夹具，覆盖并发隔离、工具往返、
空/截断/错误、旧配置、取消和旧 Chat/Gemini 回归。没有调用付费 AI 服务；
真实服务商的具体模型兼容性需使用该服务商支持的协议进行连接测试。
