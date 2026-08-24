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
  revision 等自动生成 PASS / FAIL / WARN 清单（`checks.json`）。
- **LLM 判定（非确定性）**：Agent 按 `SKILL.md` 契约读取证据包，在
  `report.md` 中填写“异常与说明”与“最终结论”（PASS / PASS_WITH_RETRY /
  FAIL）。runner 的退出码只反映“执行是否完成”（`writer_status == completed`），
  不代表用例通过与否。

## 2. 目录结构与模块职责

### 2.1 frontend/tests/e2e（Playwright）

| 文件 | 职责 |
|---|---|
| `playwright.config.ts` | 测试配置：仅匹配 `writer_effect_ui.spec.ts`、单 worker 串行、40 分钟超时、trace/video/截图全开；产物统一输出到 `tests/e2e/writer-test/reports/ui/<runId>` |
| `writer_ui_helpers.ts` | 认证注入（localStorage `lazymind:user`）、打开会话、DOM Range 文本选区模拟 |
| `writer_effect_ui.spec.ts` | 4 个串行用例：Markdown/IR 选区改写预览与落盘、手动编辑保存后写回、Markdown 写回飞书 |
| `writer_bridge.mjs` | Node 桥接脚本，被 `func_run.py` 以子进程方式调用，驱动完整写作流程并输出一行 JSON 结果 |

### 2.2 tests/e2e/shared（共享能力层）

| 模块 | 职责 |
|---|---|
| `api.py` | 认证登录（JWT）、`send_chat`（SSE 双 recipe）、分块附件上传、工作流终态轮询、最终产物读取 |
| `case_loader.py` | 用例加载（目录布局 / YAML 注册表）、飞书占位符替换、环境变量展开、执行约束注入 |
| `observability.py` | trace 获取（Langfuse / 本地 OTel JSONL/zip）与归一化；任务 SSE 流捕获与重建 |
| `feishu.py` | 通过 `lark-cli` 读取飞书文档、按基线 revision 重置文档 |
| `document_metrics.py` | 最终产物到 Markdown 的转换、可见字符统计、修订差异度量 |

### 2.3 tests/e2e/writer-test（writer 测试栈）

| 文件/目录 | 职责 |
|---|---|
| `runners/func_run.py` | 功能模式 runner：执行 + 取证 + 会话补全 + 机械检查 + 报告骨架 |
| `runners/perf_run.py` | 性能模式 runner：API 执行 + trace + 三阶段统计 |
| `analyzers/analyze_common.py` | trace 结构事实抽取（路由/步骤/工具/写回/provider/模型） |
| `analyzers/analyze_func.py` | 功能机械检查（SSE、媒体、交叉引用、编号、飞书修订） |
| `analyzers/analyze_perf.py` | 三阶段互斥性能统计与 Markdown 报告渲染 |
| `cases/*.yaml` | 用例注册表（功能 C0X/I0X/X0X、性能 P0X）与统一执行约束模板 |
| `fixtures/` | 功能/性能用例的附件素材（大纲、全文、带引用的文档） |
| `scripts/full_run.sh` | 一键编排（perf / func / both） |
| `tests/` | 分析器与 runner 的 unittest 自测 |
| `SKILL.md` / `README.md` | Agent 执行契约与使用说明 |

## 3. 用例定义与加载

### 3.1 两种用例形态

1. **YAML 注册表**（主流）：
   - `writer_func_cases.yaml`：C01–C07 / I01–I02 / X01–X03，每条场景含
     `request`（提示词 + 附件 fixture + 飞书文档）与 `expected`（路由、步骤、
     工具、槽位、写回、媒体、交叉引用等断言）。
   - `writer_perf_cases.yaml`：P01–P05，每场景 5 个 case（共 25 个）。
   - `exec_constraints.yaml`：`write` / `revise` 两套统一执行约束模板
     （只触发一次工作流、只调三个 workspace 工具、重试上限 3 次、禁止兜底等），
     由 loader 在运行时注入到提示词末尾，用例 `text` 不内嵌约束。
2. **目录布局**（兼容旧式）：`cases/<场景>/prompt_N.md` +
   可选 `attachment_N.*` / `document_N.*` / `case_N.yaml` sidecar。

### 3.2 加载与预处理（`case_loader.py`）

`Case` 值对象统一承载：场景号、提示词文本、附件路径、飞书引用、断言字典。
加载时依次处理：

1. **飞书占位符替换**：提示词中的 `${<registry_key>}`（如 `${P3_FEISHU_OUTLINE_1}`、
   `${outline}`）从 YAML 的 `feishu_docs` 注册表解析为真实 URL，同时记录
   `history_version_id` / `baseline_revision_id` 供跑前基线重置。
2. **环境变量展开**：`${ENV_VAR}` 形式，缺失即报错。
3. **执行约束注入**：`inject_exec_constraints` 把 `exec_constraints.yaml`
   中对应模板追加到提示词末尾（`constraints: write|revise`）。
4. **断言归一化**：把 YAML 的 `expected` 折叠为 loader 输出的扁平断言字典
   （`route` / `steps` / `tools_required` / `slots_required` / `write_back` /
   `media` / `cross_ref` / `ref_integrity` / `numbering` /
   `ref_number_consistency` / `provider` / `modify_types` /
   `ui_editable` / `new_revision`），功能检查统一消费这一格式。

## 4. 执行流程（一条用例的完整数据流）

以 `func_run.py` 为例（perf 流程同构、去掉 UI 与证据包）：

```text
用例加载 (case_loader)
  → Session.login（authservice 登录 + JWT 解出 user_id）
  → 附件上传（可选，temp/uploads 三段式分块）
  → 飞书基线重置（可选，lark-cli history-revert 恢复 baseline）
  → 执行通道：
      UI    : node writer_bridge.mjs（Playwright 驱动聊天页）
      no-UI : send_chat（SSE）+ 轮询 workflow-sessions:latest
  → conversation_id 解析（UI 模式回退：按创建时间从会话列表匹配）
  → 拉取最新工作流会话（有界重试，等面板 completed 落盘）
  → 读取 draft_document 槽位 → 落盘 final.md
  → 获取 trace（Langfuse/local，3 次有界重试）
  → 分析（analyze_common）+ 会话槽位/修改计划补全
  → 机械检查（run_common_checks + run_func_checks + media.display）
  → 飞书跑后 revision 拉取并回填 feishu.revision 检查
  → 落盘证据包（run_N.json / trace.json / checks.json / evidence/ / report.md）
```

关键点：

- `wait_for_writer_completion` 每 2 秒轮询一次
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
   （避免关联到旧会话的残留面板）。
6. 轮询面板 class `workflow-panel--(active|waiting|completed|failed)`，
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
8. 收集 `window.__lazymindSse`，按 URL 拼接增量块、解析 SSE 帧，把
   `artifact_stream_start / artifact_stream / _end / _abort` 归一化为
   Python 侧一致的 start/delta/end 流记录（`stream_id`、事件序列、拼接
   文本、slots、content_types、aborted），连同 task_id 一并输出。
9. 从 `sessionStorage.chat_resume_conversation_id` 或 URL 解析
   `conversation_id`，输出 JSON：
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
  稳定**才返回；一次运行可能拆成多条 ReactAgent trace，按 observation id
  去重后**合并**成一条。
- **本地 OTel**：读 `data/traces/*.jsonl`（及 .zip 归档），按
  `session.id` / `lazyllm.trace.metadata.*` / io 载荷匹配，把每行 OTel
  span 归一化为 Langfuse 形状的 observation（`_normalize_local`）。

归一化后的统一 trace 形状被分析器与统计共用，屏蔽后端差异。

### 6.4 任务 SSE 流捕获（`observability.py`）

`collect()` 订阅 `/api/core/tasks/{task_id}:stream`，把
`artifact_stream_start / _delta / _end` 按 `stream_id` 重组为
`StreamRecord`（事件序列、拼接文本、槽位、content type、abort 标记），
`TaskCapture` 同时记录 `tool_call / tool_result / artifact_update` 与
终态事件。`select_complete_streams` 只取“start → delta+ → end 且未 abort”
的完整流，供 `sse.protocol` / `sse.streaming` / `sse.reconstruction`
检查。事件名兼容新旧协议：当前后端下发 `artifact_stream`（delta），旧协议
为 `artifact_stream_delta`，统一归一到 delta 事件形态；`artifact_stream_abort`
归一到 end + abort。UI 模式下流由桥接的 XHR 补丁实时捕获（见 5.1），
no-UI 模式为 task 结束后 best-effort 订阅。

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
- `document_stats` 用 `difflib.SequenceMatcher` 计算相对原文的增删字符与
  覆盖率，对应数据表的“生成/修改文章字数”列；
- `selected_final_slots` 按优先级选择最终展示槽位
  （`delivered_markdown` > `final_document_md` > … > `draft_document`）。

## 7. 分析器与机械检查

### 7.1 analyze_common（trace 结构事实）

从归一化 trace 提取：

- `route`：create / expand / rewrite / revise / unknown。由于工作流已封装，
  路由由 `writer_draft_workspace` 输出的 `operation`
  （generate/rewrite/revise）+ `writer_prepare_workspace` 是否绑定
  `source_document` 联合判定；运行失败导致无法推断时返回 `unknown` 并给
  WARN 而非假断言。
- `step_path`：`advance_step` span 的 `attempt_results[].step_id` 按时间
  顺序去重（排除 `__start__` / `__end__`）。
- `tool_sequence`：所有 `writer_*` / `patch_artifact` / `advance_step` span
  的时序。
- `provider`：feishu / local。优先看工具 io 里的 `feishu.cn` URL，封装后
  回退到任意 span 元数据中真实 `https://…feishu.cn` URL。
- `write_back`：`writer_replace_document` / `writer_publish_revision` 计数；
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
| `final.numbering` | 编号序列正确（章节层级、图/表/代码递增），require 时不得缺失 |
| `final.reference_number_consistency` | 正文引用文字编号 == 目标当前编号（X03 当前预期 FAIL，产品已知现状） |
| `final.ui_editable` / `final.new_revision` | IR 可编辑标记、draft revision 新建 |

IR 与 Markdown 两套解析并行实现（`_count_ir_internal_refs` vs
`collect_markdown_reference_targets` 等），支持题注新旧两种格式
（“表 1：”前缀 / “1 表名”数字在前）。

### 7.3 analyze_perf（三阶段性能统计）

**阶段归属**：优先用 `advance_step` 的 step_id 构造时间窗
（prepare / outline / write_document）；旧 trace 无边界时按 workspace
函数名回退（`_fallback_phase_map`），相邻阶段以中点分界。

**三桶统计**（每个阶段同维度）：

- `full`（完整过程）：阶段窗口内全部观测，含重试、触发前探测、阶段间决策
  与终态后尾迹；
- `abnormal`（异常/尾迹）：非最后一次尝试、触发前探测、阶段间决策、终态后
  尾迹；
- `normal`（正常链路）：该阶段 workspace 工具**最后一次调用**且成功
  （level=DEFAULT）的执行；末次失败则 `normal=None` 且用例判失败。

**触发前/路由段与“干净全流程”**（飞书导出口径）：

- `pre_workflow`（触发前/路由段）：进入首个 `advance_step`（prepare 步骤）
  之前的观测，即从会话开始处理（规则画像/路由 LLM）到工作流步骤执行前的
  路由、trigger 等；已计入各阶段 `normal` 的观测会去重，避免单次成功场景
  重复累加。
- `clean_full`（干净全流程）：`pre_workflow` + 各阶段 `normal` 合计。飞书
  “全数据”表的“全流程”行取该口径：**保留路由/触发前时间，剔除重试与异常**；
  工具次数/工具时间仍按各阶段 normal 合计，章节数与生成/修改字数取
  `write_document` 阶段。旧统计无 `clean_full` 时回退 trace 全链路
  （`full_link`）。
- `stats.md` 的“全链路”列仍为 trace 全链路（`full_link`），包含触发前与
  异常/尾迹，作为原始统计保留；两者差异即“重试与异常”部分。

指标：墙钟（排除容器 span、工具时间扣除内嵌 LLM 耗时）、LLM 轮数/累计
耗时/输入输出 token 与字符、工具调用次数与实际执行时间、write_document
章节数（`draft_section_count` 或草稿 span 数回退）、生成/修改字数
（来自 `document_stats`）。

**场景聚合**（`avg_traces`）输出 `stats.json`，`render_table` 渲染两张
同维度大表 + 异常来源说明，`sheet_rows` 生成飞书数据表“全数据”导出格式
（阶段行只取 normal 链路，全流程行取 `clean_full`；末次失败整例不导出）。
`render_analysis` 给出长耗时 / 高 Token 阶段与原因的初步结论。

## 8. 会话补全（func 关键机制）

工作流封装后，trace 里不再有逐槽位保存记录，因此 runner 在分析后做两次
**从 core 会话回填**：

1. `_merge_session_slots`：以 `workflow-sessions:latest` 的 `slots` 为准，
   补全 `slot_view` 的 extension / max_revision / stage（IR 场景从
   `document_modify_plan`、`load_slot` 解析 stage）。
2. `_merge_session_modify_types`：从 `document_modify_plan` 槽位读出
   `instructions[].modify_type` 集合，补全 `modify_types` 断言所需的
   create / update / delete / move 列表。

步骤级证据（`retries.json`）优先取会话 `steps`；会话无 steps 时从 trace
的 workspace span 推导（ERROR → failed，DEFAULT → completed，按出现次数
计 attempt）。

## 9. 编排脚本（full_run.sh）

```bash
bash tests/e2e/writer-test/scripts/full_run.sh                    # 默认 perf：P01–P05
bash tests/e2e/writer-test/scripts/full_run.sh --mode func        # 功能 C01–C07/I01–I02/X01–X03
bash tests/e2e/writer-test/scripts/full_run.sh --mode both        # perf + func 共用时间戳
bash tests/e2e/writer-test/scripts/full_run.sh P03 --cases 1,2    # 指定场景/case
```

主要参数：`--base-url`、`--no-trace`、`--no-stats`/`--no-checks`、
`--recipe`（text_intent / explicit_mention）、`--venv`、`--cases-root`。

工作方式：按模式选择默认场景集 → 每个场景建
`reports/<func|perf>/<时间戳>/<场景>/` → 逐 case 调 runner，stdout 重定向
为 `run_N.json`（stderr 保留为 `run_N.stderr`）→ trace 拷贝到
`traces/case_N.json` → perf 场景用 `analyze_perf` 聚合出 `stats.json` /
`stats.md`。兼容旧目录布局（`cases/<场景>/prompt_N.md`）与 YAML 注册表两种
用例来源。

## 10. 前端 Playwright 测试（独立于 writer 栈）

`writer_effect_ui.spec.ts` 是**面向既有会话**的 UI 回归用例（串行、单
worker），通过环境变量指定会话 ID，未配置时 `test.skip`：

| 用例 | 环境变量 | 验证点 |
|---|---|---|
| Markdown 选区改写 | `AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID` | 选区 → `:action-preview` → inline diff 应用 → PATCH 槽位落盘 → 面板 completed |
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
run_N.json        运行信封（状态/耗时/会话/checks/retry_count/models/providers）
trace.json        归一化 trace（Langfuse/local）
final.md          最终 Markdown（IR 场景另有 evidence/draft_document.json）
checks.json       机械检查结果
evidence/
  expected.json   预期断言（来自 YAML）
  facts.json      trace 事实（route/steps/tools/write_back/provider/models）
  draft_document.* 最终产物
  resolved_media_assets.json  媒体资产（媒体场景）
  retries.json    重试事件 + trace ERROR span + 会话步骤状态
  sse.json        SSE 流（UI 由桥接 XHR 捕获 / no-UI best-effort）
report.md         机械检查表 + LLM 待填两节
_ui_bridge/       录屏（webm）/ 提示词（UI 模式）
```

独立运行 `writer_effect_ui.spec.ts` 时，Playwright 报告/结果输出到
`reports/ui/<runId>/playwright-report` 与 `playwright-results`。

### perf 单用例 + 场景聚合

```text
case_N/run_N.json     单 case 摘要
case_N/trace.json     归一化 trace
case_N/final.md       最终文档
case_N/document_stats.json  文档度量（生成/修改字数）
traces/case_N.json    trace 拷贝（供聚合）
stats.json / stats.md 场景级聚合与报告
```

## 13. 测试自身的单元测试

`tests/e2e/writer-test/tests/` 下 5 个 unittest 文件覆盖：

- `test_analyze_common.py`：合成 trace 的 workspace 路由/步骤/工具/provider
  推断；
- `test_analyze_func.py`：交叉引用、编号、媒体、飞书 revision 等检查；
- `test_analyze_perf.py`：advance_step 边界、三桶聚合、sheet_rows 导出；
- `test_func_run.py` / `test_perf_run.py`：CLI 参数、case 加载（约束注入、
  飞书占位符解析、附件解析）。

运行：

```bash
cd tests/e2e/writer-test && PYTHONPATH=../ \
  /Users/chensiyu2/Code/LazyMind/.venv/bin/python -m unittest discover
```

## 14. 设计要点与注意事项

1. **混合判定**：机械检查只做“证据比对”，不替产品下结论；FAIL/WARN 需要
   Agent 结合 evidence 判断是真实缺陷还是可恢复偶发异常，证据不足标
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
   的问题；事件名按新旧协议归一化后复用同一套协议/流式/重建检查。
7. **超时分层**：桥接单次工作流上限 15 分钟；runner 默认 `--timeout`
   1800s（30 分钟）；`full_run.sh` 默认 600s（10 分钟，与 SKILL 熔断
   一致）。实际以最内层先到者为准，三层不一致时按场景显式传参控制。
8. **双 recipe 保留**：`text_intent` 与 `explicit_mention` 同时可用，
   专门用于对比路由差异（历史回归点）。
9. **修改安全**：`tests/e2e_old/` 为只读备份；飞书基线变化时更新 YAML
   `feishu_docs` 的 revision/history；执行约束只改
   `exec_constraints.yaml`；脚本改动必须跑通 unittest。
