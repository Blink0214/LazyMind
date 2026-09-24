# LazyMind E2E 测试脚本技术说明

> 本文档从技术视角梳理仓库中两套端到端测试脚本的架构、实现方式与关键机制：
> `frontend/tests/e2e`（Playwright 前端层）与 `tests/e2e`（Python 测试栈）。
> 测试对象为 AI Writer 工作流（prepare / outline / write_document 三阶段，
> 已封装为 `writer_prepare_workspace` / `writer_outline_workspace` /
> `writer_draft_workspace` 三个顶层 workspace 工具）。

## 1. 总览

整个 e2e 体系是**双层结构**：

| 层 | 位置 | 技术栈 | 职责 |
|---|---|---|---|
| 前端 UI 层 | `frontend/tests/e2e` | Playwright + TypeScript | 驱动真实浏览器操作聊天 UI、验证交互链路 |
| 执行/分析层 | `tests/e2e` | Python stdlib（urllib + SSE） | 认证、发消息、等待工作流、取 trace、机械检查、性能统计 |
| 编排层 | `tests/e2e/writer-test/scripts/full_run.sh` | Bash | 一键串行运行多场景、聚合报告 |

两条执行通道：

1. **UI 模式（func 默认）**：Python runner 通过 subprocess 调用
   `writer_bridge.mjs`（Node/Playwright）驱动聊天页面，模拟真实用户操作
   （@AI Writer、上传附件、发送、等待面板完成、录屏）。
2. **no-UI 模式（perf 固定）**：Python 直接调用 Core 的
   `conversations:chat` SSE 接口发消息，轮询工作流会话，不启动浏览器。

判定采用**混合模型**：

- **机械检查（确定性）**：runner 脚本根据 trace、会话槽位、SSE 流、飞书
  revision 等自动生成 PASS / FAIL / WARN 清单（写入 `evidence.json.checks`）。
- **机械判定（确定性）**：runner 在 `run_N.json` 写入
  `mechanical_verdict`（PASS / PASS_WITH_RETRY / FAIL / INCONCLUSIVE）；FAIL
  或 INCONCLUSIVE 会返回非零退出码，供脚本与 CI 使用。
- **最终判定（业务口径）**：Agent 按 `SKILL.md` 契约读取证据包，在
  `report.md` 中填写“异常与说明”与“最终结论”。该结论用于测试表，和
  `mechanical_verdict` 是两个独立字段，可结合专项测试项口径复核机械结果。

## 2. 目录结构与模块职责

### 2.1 frontend/tests/e2e（Playwright）

| 文件 | 职责 |
|---|---|
| `playwright.config.ts` | 测试配置：仅匹配 `writer_effect_ui.spec.ts`、单 worker 串行、40 分钟超时、trace 失败保留、video 全量保留、截图关闭；产物统一输出到 `tests/e2e/writer-test/reports/ui/<runId>` |
| `writer_ui_helpers.ts` | 认证注入（localStorage `lazymind:user`）、UI selector 集中定义、打开会话、DOM Range 文本选区模拟 |
| `writer_effect_ui.spec.ts` | 4 个串行用例：Markdown/IR 选区改写预览与落盘、手动编辑保存后写回、Markdown 写回飞书 |
| `writer_bridge.mjs` | Node 桥接脚本，被 `func_run.py` 以子进程方式调用，驱动完整写作流程并输出一行 JSON 结果 |

### 2.2 tests/e2e/shared（共享能力层）

| 模块 | 职责 |
|---|---|
| `api.py` | 认证登录（JWT）、`send_chat`（SSE 双 recipe）、分块附件上传、工作流终态轮询、最终产物读取 |
| `case_loader.py` | 功能/性能 YAML 注册表加载、飞书占位符替换、环境变量展开、执行约束注入 |
| `execution.py` | 两个 Runner 共用的登录、provider preflight、附件上传、API 发送、会话关联和 trace 落盘生命周期 |
| `preflight.py` | Provider 基线 fail-fast 恢复；失败时禁止发送 Writer 请求 |
| `observability.py` | trace 获取（Langfuse / 本地 OTel JSONL/zip）与归一化；任务 SSE 流捕获与重建 |
| `feishu.py` | 通过 `lark-cli` 读取飞书文档、按基线 revision 重置文档 |
| `document_metrics.py` | Markdown/IR artifact 归一化、IR block 遍历、最终产物到 Markdown 的转换、可见字符统计、修订差异度量 |

### 2.3 tests/e2e/writer-test（writer 测试栈）

| 文件/目录 | 职责 |
|---|---|
| `runners/func_run.py` | 功能模式 runner：执行 + 取证 + 会话补全 + 机械检查 + 报告骨架 |
| `runners/perf_run.py` | 性能模式 runner：API 执行 + trace + 三阶段统计 |
| `analyzers/analyze_common.py` | trace 中的工具、workspace、写回和模型事实；route/steps/slots/provider 由 session 提供 |
| `analyzers/analyze_func.py` | 功能机械检查（SSE、媒体、交叉引用、编号、飞书修订） |
| `analyzers/analyze_perf.py` | 三阶段互斥性能统计与 Markdown 报告渲染 |
| `cases/*.yaml` | 用例注册表（功能 C0X/I0X/X0X、性能 P0X）与统一执行约束模板 |
| `fixtures/` | 功能/性能用例的附件素材（大纲、全文、带引用的文档） |
| `scripts/full_run.sh` | 一键编排（perf / func / both） |
| `tests/` | 分析器与 runner 的 unittest 自测 |
| `SKILL.md` / `README.md` | Agent 执行契约与使用说明 |

## 3. 用例定义与加载

### 3.1 两套 YAML 用例

- `writer_func_cases.yaml`：C01–C08 / I01–I02 / X01–X03，每条场景含
  `request`（提示词 + 附件 fixture + 飞书文档）与 `expected`（路由、步骤、
  工具、槽位、写回、媒体、交叉引用等断言）。
- `writer_perf_cases.yaml`：P01–P05，每场景 5 个 case（共 25 个）。
- `exec_constraints.yaml`：`write` / `revise` 两套统一执行约束模板
  （只触发一次工作流、只调三个 workspace 工具、重试上限 3 次、禁止兜底等），
  由 loader 在运行时注入到提示词末尾，用例 `text` 不内嵌约束。

功能与性能的用例、Runner 和判定口径保持独立，只复用基础执行生命周期。旧版
`cases/<场景>/prompt_N.md` 目录输入已移除，避免同一性能场景出现两个事实来源。

### 3.2 加载与预处理（`case_loader.py`）

`Case` 值对象统一承载：场景号、提示词文本、附件路径、飞书引用、断言字典。
加载时依次处理：

1. **飞书占位符替换**：提示词中的 `${<registry_key>}`（如 `${P3_FEISHU_OUTLINE_1}`、
   `${outline}`）从 YAML 的 `feishu_docs` 注册表解析为真实 URL，同时记录
   `history_version_id` / `baseline_revision_id` 供跑前基线重置。
2. **环境变量展开**：`${ENV_VAR}` 形式，缺失即报错。
3. **用户输入保持原文**：不再追加 `exec_constraints.yaml` 执行约束；`constraints: write|revise`
   仅保留为文档度量元数据。
4. **断言归一化**：把 YAML 的 `expected` 折叠为 loader 输出的扁平断言字典
   （`route` / `steps` / `tools_required` / `slots_required` / `write_back` /
   `media` / `cross_ref` / `ref_integrity` / `numbering` /
   `ref_number_consistency` / `provider` / `modify_types` /
   `ui_editable` / `new_revision`），功能检查统一消费这一格式。

## 4. 执行流程（一条用例的完整数据流）

`shared/execution.py` 只抽取两套 Runner 真正相同的生命周期；UI/no-UI 等待、
功能证据生成和性能统计仍留在各自 Runner。以功能用例为例：

```text
用例加载 (case_loader)
  → prepare_execution
      → Session.login（authservice 登录 + JWT 解出 user_id）
      → 飞书基线重置（可选；失败立即终止）
      → 附件上传（可选，temp/uploads 三段式分块）
  → 执行通道：
      UI    : node writer_bridge.mjs（Playwright 驱动聊天页）
      no-UI : send_chat（SSE）+ 轮询 workflow-sessions:latest
  → conversation_id 关联（必须来自本次 conversations:chat SSE，URL /
    sessionStorage 仅作交叉校验；新面板 session_id 必须与 Core
    latest-session 同时存在且一致）
  → resolve_workflow_session（有界重试 + UI/Core session 严格关联）
  → 读取 draft_document 槽位 → 落盘 final.md
  → collect_trace_evidence（Langfuse/local，3 次有界重试并落盘）
  → 分析（analyze_common）+ 会话槽位/修改计划补全
  → 机械检查（run_common_checks + run_func_checks + media.display）
  → 飞书跑后 revision 拉取并回填 feishu.revision 检查
  → 落盘证据包（run_N.json / trace.json / evidence.json / report.md）
```

关键点：

- `wait_for_writer_completion` 默认每 10 秒轮询一次
  `workflow-sessions:latest`，`completed / failed / stopped` 视为终态；
  `waiting` 在当前运行时是**自动推进**的瞬态，不发送“继续”审批。
- 工作流刚完成时 latest-session 可能短暂不可见，runner 做 10 次 × 5 秒
  有界重试。
- trace 获取对 Langfuse 偶发 422/5xx 做 3 次重试，避免单次瞬态错误导致
  整份分析缺失。

## 5. 双执行通道

| 能力 | UI（Playwright 桥接） | no-UI（纯 API + SSE） |
|---|---|---|
| 路由/工具/步骤/槽位/写回/媒体/交叉引用/编号检查 | ✅ | ✅ |
| 飞书基线重置 / 写回 / revision 校验 | ✅ | ✅ |
| `media.display`（浏览器实际渲染 + 失败图检测） | ✅（渲染数 + 0 尺寸/加载失败图清单） | WARN |
| 阶段面板跟随录屏、桥接 `retry_events` | ✅ | 无（靠 trace ERROR + 会话步骤） |
| SSE 协议 / 流式效果 / 重建检查 | ✅（桥接 XHR 实时捕获） | best-effort（task_id 可查时捕获） |

### 5.1 UI 通道（`writer_bridge.mjs`）

子进程协议：Python 侧以 `node writer_bridge.mjs --scenario X --case N
--prompt-file ...` 启动，桥接脚本最终在 stdout 输出**一行 JSON**，Python
解析后继续分析。桥接内部步骤：

1. 启动 Chromium（无头，1920×1080，全程录屏到 `_ui_bridge/`）。
2. 注入认证并挂 XHR 捕获补丁：`addInitScript` 写 localStorage
   `lazymind:user`（token + userId）模拟已登录态，同时重写
   `XMLHttpRequest.prototype.open/send`，把 `/api/core/tasks/{id}:stream`、
   `conversations:chat`、`workflow-sessions/{id}/events` 的每次增量
   `responseText` 追加记录到 `window.__lazymindSse`（前端 SSE 客户端是
   XHR 实现，原生 EventSource 的 CDP 事件不会触发）。
3. 打开 `/agent/chat/home`，输入 `@AI Writer` 并选中 mention。
4. 可选附件上传：等待文件 chip 出现，观察发送按钮 `disabled → enabled`
   状态迁移确认上传完成；上传失败（chip 消失）则直接报错。
5. 发送提示词后，等待一个 `data-session-id` **不在发送前快照中的**新面板
   （避免关联到旧会话的残留面板）；90 秒内未出现则失败，
   不回退到任意可见面板。
6. 轮询 Core workflow-session 状态，并读取面板 class 作为 UI 展示证据和
   API 不可用时的兼容兜底，
   最长 15 分钟：
   - **步骤面板跟随**：步骤徽标从空变为非空（running/interrupted/failed）
     时点击对应 tab，让录屏跟随当前执行阶段。
   - **waiting**：若出现可点击的主操作按钮（`workflow-panel__action-btn--primary`），
     自动点击继续。
   - **failed 自愈观察**：记录 failed 时间，若 30 秒内未恢复则视为稳定失败
     终止；期间 `failed → running` 记 `step_retried` 事件。
7. completed 后切到最后一个步骤面板，分类统计正文图片：已渲染
   （`complete && naturalWidth > 0 && naturalHeight > 0`）、失败/异常
   （未加载完成、0 尺寸、解码失败，前端会显示为坏图）、未解析
   `media-placeholder://` 占位图，分别输出数量与来源清单。
8. 收集 `window.__lazymindSse`，按 URL 拼接增量块并解析当前协议的 JSON
   `data.type` 事件。桥接只回传 task event；UI 与 no-UI 两条通道统一由
   `observability.py` 将 `artifact_stream_start / artifact_stream / _end /
   _abort` 归一为 start/delta/end 流记录。
9. 从本次 `conversations:chat` SSE 提取权威 `conversation_id`（缺失则失败），
   并与 `sessionStorage.chat_resume_conversation_id`、URL 中的 ID 交叉校验；不一致
   直接失败。输出 JSON：
   `conversation_id / session_id / writer_status / rendered_images /
   failed_images / sse_capture / retry_events / captured_steps /
   base_url / scenario / case`。

### 5.2 no-UI 通道（`shared.api`）

`send_chat` 提供**双信号 recipe**，用于对比两种路由方式：

- `text_intent`（默认）：只发文本，靠提示词中的 “Writer / 工作流” 让路由
  器从工作流目录选中 writer-plugin，不带显式 mention。
- `explicit_mention`：携带 `mentions=[{"type":"workflow",
  "resource_id":"builtin:writer-workflow"}]`，强制路由到 writer 工作流。

请求体为 `POST /api/core/conversations:chat` 的 SSE 流（`stream: true`），
包含 `input` 列表（文本 + 已上传文件 URI）、`thinking_depth: high`、
`environment_context`（locale/时间/时区）等。SSE 消费端解析 `data:` 行，
提取 `conversation_id` 与 `finish_reason`，收到
`FINISH_REASON_STOP` 即停。续跑/审批复用同一入口，携带
`conversation_id` 与 `workflow_context`。

## 6. 共享能力层详解

### 6.1 认证（`api.py`）

- `Session.login`：`POST /api/authservice/auth/login` 拿 access_token，
  再从 JWT payload 解出 `user_id`（与旧 benchmark 脚本解析口径一致）。
- 凭据来源：`LAZYMIND_E2E_USERNAME/PASSWORD` → 默认 `admin/admin`
  （旧名 `WRITER_BENCH_*` 已合并移除）。

### 6.2 附件上传（`api.py`）

镜像前端真实路径的三段式上传（`temp/uploads:initUpload` → 按 5MB 分块
`PUT parts/{n}` → `:complete`），返回的 `stored_path` 直接作为后续 chat
的 `file` input，不绕过产品代码路径。

### 6.3 trace 获取与归一化（`observability.py`）

后端由环境决定（`LAZYLLM_TRACE_CONSUME_BACKEND`，`auto` 时再回退
`LAZYLLM_TRACE_BACKEND`，默认 langfuse）：

- **Langfuse**：分页拉 `api/public/traces`，按 `sessionId` 前缀匹配
  conversation_id / workflow_session_id；异步导出需**连续三次轮询内容
  稳定**且 span 已结束、usage 已齐才返回（稳定签名包含结束时间、usage 和父链）；
  无 sessionId 的候选只在运行时间范围内按明确会话/工作流标识匹配；
  已匹配 trace 也会按 span 时间过滤，排除同一会话追加的后续调用；
  一次运行可能拆成多条 ReactAgent trace，按 observation id
  去重后**合并**成一条。
- **本地 OTel**：读 `data/traces/*.jsonl`（及 .zip 归档），按
  `session.id` / `lazyllm.trace.metadata.*` / io 载荷匹配，把每行 OTel
  span 归一化为 Langfuse 形状的 observation（`metadata.attributes`）；
  与 Langfuse 共用完成度/稳定性检查，不按文件修改时间猜测归属。

归一化后的统一 trace 形状被分析器与统计共用，屏蔽后端差异。

### 6.4 任务 SSE 流捕获（`observability.py`）

`collect()` 订阅 `/api/core/tasks/{task_id}:stream`，把
`artifact_stream_start / artifact_stream / _end` 按 `stream_id` 重组为
`StreamRecord`（事件序列、拼接文本、槽位、content type、abort 标记），
`TaskCapture` 同时记录 `tool_call / tool_result / artifact` 与
终态事件。功能检查保留所有匹配的 draft 流，让 `sse.protocol` 能明确报告
缺失 start/end 或 abort；需要完整最终文本的其他消费者仍可使用
`select_complete_streams`。当前后端的 `artifact_stream` 归一为内部 delta 事件；
`artifact_stream_abort` 归一到 end + abort。UI 模式下流由桥接的 XHR 补丁
实时捕获（见 5.1），
no-UI 模式在首次观察到 workflow session 后并行订阅 workflow event stream，
并对订阅前已经结束的 task 做快照回放。

### 6.5 飞书基线重置（`feishu.py`）

通过本机已登录的 `lark-cli`：

1. 读取基线 revision 与当前 revision 的 Markdown 正文；
2. 内容相同则跳过；不同则 `docs +history-revert` 恢复到
   `history_version_id`，轮询任务状态到 done；
3. 恢复后再次读取并比对基线内容，不一致则报错。

这保证每次跑飞书用例前文档处于已知状态，写回类断言（revision 严格递增 /
不变）可重复执行。

### 6.6 文档度量（`document_metrics.py`）

- IR（blocks 结构）↔ Markdown 双向转换（`writer_document_to_markdown` /
  `inline_artifact_to_markdown`）；
- `visible_text` 剥离 Markdown 语法得到“可见字符”口径；
- 性能 Runner 仅保存最终全文的可见字符数，对应导出的“全文字符数”列；
- `selected_final_slots` 按优先级选择最终展示槽位
  （`delivered_markdown` > `final_document_md` > … > `draft_document`）。

## 7. 分析器与机械检查

### 7.1 analyze_common（trace 结构事实）

从归一化 trace 提取：

- `tool_sequence`：所有 `writer_*` / `patch_artifact` / `advance_step` span
  的时序。
- `workspace_facts`：workspace span 直接输出的 operation、representation、
  structure_mode 与 next_step。
- `write_back`：`writer_write_document` / `writer_publish_revision` 计数；
  新工作流下写回结果在 `writer_draft_workspace` 输出的
  `document_write_result` 中，按 operation 归因工具
  （revise → publish_revision，其余 → replace_document）。
- `models / providers / llm_call_count / image_call_count`：从 `llm` 与
  `image_generator` span 统计，模型名优先取 `resolved_prompt.model`。

`run_checks` 只对 `expected` 中**存在的键**做断言（无 fail-by-default
语义），产出统一的 `Check(name, status, detail)`。

### 7.2 analyze_func（功能机械检查）

| 检查 | 内容 |
|---|---|
| `sse.protocol` | 完整流的 start→delta→end 协议、单槽位、未 abort（UI 与 no-UI） |
| `sse.streaming` | 流式效果：完整 draft 流至少 2 个 delta 帧且拼接文本非空，证明是增量到达而非一次性下发（UI 与 no-UI） |
| `sse.reconstruction` | 拼接流文本 == 最终产物全文（UI 与 no-UI） |
| `feishu.revision` | 跑前/跑后 revision 严格递增 / 不变 / any |
| `media` / `media.display` | 正文图片数（Markdown 语法或 IR image block）、未解析占位符、生成资产（来源要求已放宽）；UI 模式额外验证浏览器渲染，且渲染数达标同时**失败/异常图必须为 0** |
| `final.cross_references` | `#block-*` 链接 / IR `internal_ref` 数量，可选源文档引用保留 |
| `final.reference_integrity` | 章节/图/表/代码块锚点+题注+编号覆盖，悬空引用列出 |
| `final.numbering` | 被编号项的编号序列正确（章节层级、图/表/代码递增），require 时不得缺失；正文引用文字不要求编号 |
| `final.reference_number_consistency` | 正文引用文字编号 == 目标当前编号；用于移动章节后引用同步检查 |
| `final.ui_editable` / `final.new_revision` | IR 可编辑标记、draft revision 新建 |

IR 的 envelope 解包、block 递归、可见文本、标题和图片统一由
`shared/document_metrics.py` 提供；IR 引用语义与 Markdown 语法仍分别断言，
支持题注新旧两种格式
（“表 1：”前缀 / “1 表名”数字在前）。

### 7.3 analyze_perf（三阶段性能统计）

**唯一口径说明**见 [README 当前性能统计口径](README.md#当前性能统计口径)。

主报表及导出保持原有指标，使用 `总值(失败重试部分)`，不增加异常行列。
`reported_full` / `phases.*.reported` 保留成功样本的全部尝试，`failed_retry` 为其中
可靠识别的异常部分；`clean_full` / `normal` 保留用于审计。显示文本不能直接用于数值公式。

阶段是 advance_step 完整执行子树：以 workspace 父链识别步骤，包含步骤内部 Agent 决策。
步骤外发起决策只计全流程；不使用前端生命周期、workspace 区间或旧时间窗兜底。
跨 trace 子 Agent 关联、决策关联分别保留 `step_links`、`decision_links` 审计。
IO 关闭时仍可按执行结构关联失败发起决策；若存在输出则校验其工具名和阶段。
失败过滤所需的关联有歧义或冲突时写入 `unresolved_dispatches`，禁止正式导出。字符累计已采集载荷长度，截断片段按原样计数，
缺失部分不计入；不代表完整输入/输出字符总数，不影响其他指标导出。

normal 仅保留最后且成功的尝试；其步骤外发起决策只进入 clean_full，
步骤内部的成功决策保留在阶段中。同一步 workspace 重试也遵循成功尝试过滤。
累计 usage 先差分再过滤；墙钟按区间并集剔除失败独占时间，保护与正常执行重叠的
部分。LLM 耗时累计允许超过墙钟。正常主 Agent 调用即使没有阶段归属，也保留在
clean_full；它与 runner 总墙钟是两个指标，不能互相替换。

新增 `lazyllm.diagnostics.llm` 保存完整模型输入字段、格式化输出和响应计时，
不预计算分项字符。它用于离线归因，不替代现有 IO 字符累计、span 墙钟或业务重试口径；
详见 [完整 LLM 诊断采集](README.md#完整-llm-诊断采集不改变统计口径)。

导出共 15 列，键前增加修改文章字数。全文字符数来自 document_stats 中最终文档的可见文本。P04/P05 在 trace 回收后，从当次完整 material-analysis 输入恢复原文（飞书含资源标题），保存原文与来源哈希；可见字符差异以新增＋删除计算，段落搬移计两侧，不累计失败草稿。原文缺失/冲突则修改量缺失，成功样本中任一缺失时聚合值也缺失。失败样本不参与正常链路聚合。

### 7.4 判决模型

同一次功能运行有三层结果，不能混用：

1. `evidence.json.checks` 的单项 `PASS / FAIL / WARN`：某条机械断言的结果；
2. `run_N.json.mechanical_verdict`：Runner/CI 技术判决，取值为
   `PASS / PASS_WITH_RETRY / FAIL / INCONCLUSIVE`；
3. 测试表最终判决：Agent/人工根据本次证据，按测试表中该行的专项范围独立填写。

机械判决采用 fail-closed：工作流未完成或任一机械检查 FAIL 时为 `FAIL`；没有
FAIL 但存在 WARN/必要证据缺失时为 `INCONCLUSIVE`；真实工作流步骤发生失败并
重试成功时可为 `PASS_WITH_RETRY`。它用于退出码和问题发现，不直接覆盖测试表。

测试表专项边界：

| 测试项 | 影响该行判决的检查 |
|---|---|
| 路由/工具链 | route、tools、steps、workspace、slot、write_back、provider、revision |
| 流式输出 | `sse.*` |
| 图片能力 | media、media.display、provider media、图片与图注完整性 |
| 交叉引用 | cross_references、reference_integrity、numbering、reference_number_consistency 及 provider 物化 |

`final.content`、`final.ui_visible` 或取证链异常等非当前专项问题仍写入失败描述和
备注，但不改变另一专项行的结果。Provider/UI/会话关联失败属于测试基础设施错误；
缺少足够证据时应标为 `INCONCLUSIVE` 或测试表中的 `ERROR/BLOCKED`，不能推断产品
通过。性能模式只聚合成功用例，失败用例保留失败记录但不进入正常链路统计。

## 8. 会话补全（func 关键机制）

工作流封装后，trace 里不再有逐槽位保存记录，因此 runner 在分析后以
**从 core 会话回填**：

1. `_merge_session_slots`：以 `workflow-sessions:latest` 的 `slots` 为准，
   补全 `slot_view` 的 extension / max_revision / stage / provider；
   source/target provider 分别读取实际产物的 `provider_binding.provider` /
   `adapter`，不能误用仅为最终稿填充的槽位顶层 provider；
   representation 只使用 document descriptor/content_type，stage 只使用产物
   元数据，不根据槽位名或文件名推断。
   list 槽位数量按已选中记录的不同 `list_index` 计算，不从文件名或 IR block
   数量推断。
2. `_merge_session_steps`：读取 session `steps[].step_id`。
3. `_merge_session_route`：读取 `writer_command.action`，作为功能 route 判决。
4. `_merge_session_modify_types`：从 `document_modify_plan` 槽位读出
   `instructions[].modify_type` 集合，补全 `modify_types` 断言所需的
   create / update / delete / move 列表。

步骤级证据优先取会话 `steps`；会话无 steps 时从 trace
的 workspace span 推导（ERROR → failed，DEFAULT → completed，按出现次数
计 attempt）。

## 9. 编排脚本（full_run.sh）

```bash
bash tests/e2e/writer-test/scripts/full_run.sh                    # 默认 perf：P01–P05
bash tests/e2e/writer-test/scripts/full_run.sh --mode func        # 功能 C01–C08/I01–I02/X01–X03
bash tests/e2e/writer-test/scripts/full_run.sh --mode both        # perf + func 共用时间戳
bash tests/e2e/writer-test/scripts/full_run.sh P03 --cases 1,2    # 指定场景/case
```

主要参数：`--base-url`、`--no-trace`、`--no-stats`/`--no-checks`、
`--recipe`（text_intent / explicit_mention）、`--venv`；`--cases` 只筛选
性能 case。

工作方式：按模式选择默认场景集 → 每个场景建
`reports/<func|perf>/<时间戳>/<场景>/` → 逐 case 调 runner；runner 自己写权威
`run_N.json`，重复 stdout 和空 stderr 自动删除，非空 stderr 保留 → 仅 perf
把 trace 拷贝到 `traces/case_N.json` → 用 `analyze_perf` 聚合出 `stats.json` /
`stats.md`。功能与性能分别从各自 YAML 注册表加载。

## 10. 前端 Playwright 测试（独立于 writer 栈）

`writer_effect_ui.spec.ts` 是**面向既有会话**的 UI 回归用例（串行、单
worker），通过环境变量指定会话 ID，未配置时 `test.skip`：

| 用例 | 环境变量 | 验证点 |
|---|---|---|
| Markdown 选区改写 | `AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID` | 选区 → `:action-preview` → 新旧 diff 非空且不同 → PATCH 槽位落盘 → 正文 DOM 更新 → 重新打开会话后内容仍存在 |
| IR 选区改写 | `AI_WRITER_IR_EFFECT_CONVERSATION_ID` | 同上，IR 编辑器路径 |
| 手动编辑保存后写回 | `AI_WRITER_WRITE_BACK_CONVERSATION_ID` | 键盘编辑 → Ctrl/Cmd+S 触发 PATCH → 写回飞书状态 synced |
| Markdown 写回飞书 | `AI_WRITER_MARKDOWN_WRITE_BACK_CONVERSATION_ID` | 写回响应 `data.status == synced`、`feishu_synced`、`artifact_saved` |

认证解析顺序：`POST /_local/admin-session`（本地代理注入的 bootstrap
admin）→ 密码登录 `/api/authservice/auth/login` → 环境变量
`LAZYMIND_ACCESS_TOKEN` / `LAZYMIND_USER_ID` 兜底。Playwright 配置把
报告/产物统一写到 `tests/e2e/writer-test/reports/ui/<AI_WRITER_E2E_RUN_ID>`
（不再使用仓库外 `test-artifacts`）。

## 11. 环境变量一览

| 变量 | 用途 | 默认 |
|---|---|---|
| `LAZYMIND_BASE_URL` | 被测服务地址 | `http://localhost:8090`（frontend 默认 `http://127.0.0.1:8090`） |
| `LAZYMIND_E2E_USERNAME/PASSWORD` | 密码登录（UI/API） | `admin/admin` |
| `LAZYMIND_USER_ID` / `LAZYMIND_ACCESS_TOKEN` | 非本地部署的认证兜底 | — |
| `AI_WRITER_E2E_RUN_ID` | Playwright 产物目录标识 | 自动生成时间戳 |
| `AI_WRITER_*_CONVERSATION_ID` | 前端 spec 指定既有会话 | —（未设则跳过） |
| `PLAYWRIGHT_HEADED` | 是否有头运行浏览器 | 0 |
| `LAZYLLM_TRACE_CONSUME_BACKEND` | trace 后端：langfuse / local | langfuse |
| `LAZYLLM_TRACE_LOCAL_STORAGE_DIR` | 本地 trace 目录 | `data/traces` |
| `LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY` | Langfuse 访问凭据 | localhost:3000 |
| `WRITER_ENV_FILE` | 环境文件路径 | 仓库根 `.env` |

## 12. 输出产物

### func 单用例（`reports/func/<时间戳>/<场景>/case_1/`）

```text
run_N.json        紧凑运行信封（状态/耗时/会话/retry_count/models/providers）
trace.json        归一化 trace（Langfuse/local）
final.md          最终 Markdown
evidence.json     expected + facts + checks + retries + SSE；按需包含渲染、
                  provider materialization 与脱敏媒体证据
draft_document.json  IR 原始产物（仅 IR 场景）
export_document.md   渲染后的 Markdown（仅需物化检查时）
report.md         机械检查表 + LLM 待填两节
_ui_bridge/       录屏（webm，成功或失败均保留）
```

桥接提示词只作为进程间临时输入，运行后删除；不生成最终状态截图。Runner 成功
写入 `run_N.json` 后删除重复 stdout，空 stderr 也会删除，非空 stderr 保留。

独立运行 `writer_effect_ui.spec.ts` 时，Playwright 报告/结果输出到
`reports/ui/<runId>/playwright-report` 与 `playwright-results`。

### perf 单用例 + 场景聚合

```text
case_N/run_N.json     单 case 摘要
case_N/trace.json     归一化 trace
case_N/final.md       最终文档
case_N/document_stats.json  文档度量（最终全文可见字符数）
traces/case_N.json    trace 拷贝（供聚合）
stats.json / stats.md 场景级聚合与报告
```

## 13. 设计要点与注意事项

1. **混合判定**：机械检查只做“证据比对”，不替产品下结论；FAIL/WARN 需要
   Agent 结合 `evidence.json` 判断是真实缺陷还是可恢复偶发异常，证据不足标
   “证据不足”而非臆测。
2. **trace 只是证据之一**：槽位、revision、SSE 字节、浏览器渲染分属不同
   数据源，刻意互相独立，防止“trace 缺失 = 全部不可判定”。
3. **灰度降级**：`--no-trace` / `--no-checks` / `--no-stats` 允许在
   观测后端不可用时继续执行；缺失证据一律 WARN，不产生假 FAIL。
4. **trace 异步导出的稳定性等待**：Langfuse/本地导出需要连续 3 次内容
   稳定才返回，避免拿到半截 trace 就出报告。
5. **UI 桥接的自愈语义**：面板 failed 后给 30 秒观察窗口区分“会话自愈
   重试”与“稳定失败”；每次运行的 retry 事件落盘，供重试归因。
6. **UI 模式的 SSE 证据**：前端 SSE 基于 XHR，桥接通过注入
   `XMLHttpRequest` 补丁实时捕获任务流，避免“跑完再订阅”只能拿到快照
   的问题；会话完成后最多做 30 秒有界收尾等待，正文终止后静默 500 ms 才收尾；
   桥接另从浏览器首次 task 请求起建立独立只读订阅，保留在 observer 证据中，
   不与浏览器流合并。桥接回传当前协议事件后复用
   Python 的协议/流式/重建检查。等待超时不会补造 end，协议检查仍会失败。
7. **超时分层**：`full_run.sh` 默认给单用例 600 秒，并把同一上限传给 Runner
   和 UI 桥接；直接运行时 func 默认 600 秒、perf 默认 1800 秒。实际以最内层
   先到者为准，可按场景显式传参控制。
8. **双 recipe 保留**：`text_intent` 与 `explicit_mention` 同时可用，
   专门用于对比路由差异（历史回归点）。
9. **修改安全**：`tests/e2e_old/` 为只读备份；飞书基线变化时更新 YAML
   `feishu_docs` 的 revision/history；执行约束只改
   `exec_constraints.yaml`；脚本改动必须跑通 unittest。
10. **契约分层**：多数功能场景只断言 route、session steps、slot 和用户结果；
    精确 workspace/tool 链保留在 C01/C08（创建链路）与 C04（直接重写链路）三条
    架构契约用例中，避免内部 span 重命名击穿全部功能场景。
