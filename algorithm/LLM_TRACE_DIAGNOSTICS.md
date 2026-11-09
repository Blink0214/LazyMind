# LLM 性能诊断采集

在算法服务（包括执行子 Agent 的 worker）启动前设置：

```sh
export LAZYMIND_LLM_TRACE_DIAGNOSTICS=true
export LAZYMIND_LLM_TRACE_PAYLOAD_MAX_CHARS=0
```

重启服务后生效，仍需正常开启 LazyLLM tracing 并配置 exporter。默认关闭。
该开关独立于 `debug_capture_payload`，覆盖预分析、主 Agent 和子 Agent。
正文可能包含用户文档，按现有 trace 的访问权限管理；不采集 HTTP 认证头。

每个 LLM span 的 `lazyllm.diagnostics.llm` 属性保存 JSON：

- `attempts[].request`：在供应商请求组装完成后、HTTP 发送前快照
  `model/messages/system/prompt/input/tools/functions/tool_choice/response_format`
  字段。它是模型输入字段的快照，不是完整 HTTP 请求。
- 不预计算消息或工具的分项字符数；需要时从完整请求 JSON 离线分析。
  字符比例不等于精确 token 比例，总 token 仍来自原有供应商 usage。
- 正文保存在 `json`，附 `original_chars/captured_chars/truncated/sha256`。
  上限为 0 时本地不截断；正数时限制每份请求/输出 JSON 的字符数。
  截断后的 `json` 是文本片段，不保证可再次解析为 JSON。
  不受旧版 IO 每字符串 8192 字符限制，但 exporter/远端仍可能施加限制，
  拉取后应核对字符数和 SHA-256，不能仅凭本地 `truncated=false` 断言远端完整。
- `output`：成功调用格式化后的输出；不是原始 HTTP 响应。
- `request_preparation_ms`：LLM span 采集开始至首次请求快照开始。
- `attempts[].capture_ms`：输入快照的诊断开销。
- `started_offset_ms`：每次 HTTP 尝试相对 LLM 采集开始的偏移，包含之前的重试和等待。
- `headers_ms`：发送至收到响应头；`first_body_chunk_ms`：发送至迭代器交出
  首个非空响应行（受 requests 缓冲影响，不是网卡收到首字节的时间）。
- `first_semantic_chunk_ms`：流式 OpenAI 兼容响应中首次解析出正文、推理内容或
  工具调用的时间，忽略只有角色/心跳的帧；非流式或不支持该解析器时为空。
- `duration_ms`：本次 HTTP 尝试及响应处理时间；失败保留 `error_type`，
  不保存异常消息。各次重试单独记录，不相加冒充单次首响应时间。

计时均为客户端单调时钟，不能单独区分供应商排队和网络耗时。
现有 e2e trace 拉取会保留 observation metadata；现有 16 列统计口径不变。
此功能只补充启用后的调用，不能恢复 PT0004/PT0005 已缺失的输入正文。
