# AI Writer E2E 测试

本目录只测试长文写作。功能测试与性能测试使用两套独立的 YAML 用例和 Runner：

- 功能：`cases/writer_func_cases.yaml`，C01–C07、I01–I02、X01–X03；
- 性能：`cases/writer_perf_cases.yaml`，P01–P05，每个场景 5 个 case；
- 用户输入仅使用用例 YAML 的 `text`（解析资源占位符），不追加执行约束词。
`constraints` 元数据仍用于区分写作/修订的文档度量；`exec_constraints.yaml` 不再自动注入。

架构、证据来源和判决语义见
[`e2e-test-architecture.md`](e2e-test-architecture.md)；Agent 操作约束见
[`SKILL.md`](SKILL.md)。

## 运行前准备

- LazyMind 服务可从 `http://localhost:8090` 访问；可用
  `LAZYMIND_BASE_URL` 或 `--base-url` 覆盖。
- 默认使用 `admin/admin` 登录；可用
  `LAZYMIND_E2E_USERNAME`、`LAZYMIND_E2E_PASSWORD` 覆盖。
- 飞书场景要求本机已安装并登录 `lark-cli`。Runner 会在发送 Writer 请求前
  自动恢复 YAML 中登记的文档基线；恢复失败时用例立即停止。
- UI 模式需要 `frontend` 已安装 Playwright 及浏览器依赖。

## 功能测试

默认通过 Playwright 驱动真实聊天 UI：

```bash
# 全部功能场景
bash tests/e2e/writer-test/scripts/full_run.sh --mode func

# 单场景
bash tests/e2e/writer-test/scripts/full_run.sh --mode func C03

# 跳过浏览器，直接走 Core API
bash tests/e2e/writer-test/scripts/full_run.sh --mode func --no-ui C01
```

直接运行单个功能场景：

```bash
.venv/bin/python tests/e2e/writer-test/runners/func_run.py \
  --scenario X01 \
  --base-url http://localhost:8090 \
  --output-dir /tmp/writer-test/X01
```

功能场景始终来自 `writer_func_cases.yaml`，没有 case 编号参数。

功能 UI 状态每 15 秒轮询。SSE 从页面发送请求前安装捕获：浏览器 XHR 与独立
只读 task 订阅分开取证；独立订阅在浏览器首次请求该 task stream 时启动，
不等工作流结束后再补订阅。单例桥接预算预留 35 秒用于收尾；完成后最多等待
30 秒，并要求正文终止事件后至少 500 ms 没有新事件。收尾超时不会补造 end，
最终页面正文也不会替代捕获流。`_ui_bridge/sse_capture.json` 保留两路事件及连接
开始/断开时间、断开类型、收尾原因；`evidence.json.sse.observer` 保留独立订阅的
归一化证据。机械 SSE 检查仍使用浏览器捕获，不用 observer 修补浏览器缺失。

source_document 的 provider 从 IR `provider_binding.provider` 读取，
target_document 从 `adapter` 读取；最终稿才优先使用 Core 槽位顶层的写回
`provider`。修订集按实际 `PatchSet` / `StringReplaceSet` schema 检查，
不要求该模型不存在的 `patch_type` 字段。

X01/X02 的编号只检查章节标题、图注等被编号项；正文链接文字无需重复编号。
引用仍需满足数量要求并准确指向存在的目标。

## 性能测试

性能模式固定走 Core API，不启动浏览器：

```bash
# 全部 P01–P05，每个场景 5 个 case
bash tests/e2e/writer-test/scripts/full_run.sh

# 指定场景和 case
bash tests/e2e/writer-test/scripts/full_run.sh P03 --cases 1,2

# 功能与性能串行执行，共用一次运行时间戳
bash tests/e2e/writer-test/scripts/full_run.sh --mode both
```

直接运行一个性能 case：

```bash
.venv/bin/python tests/e2e/writer-test/runners/perf_run.py \
  --scenario P01 \
  --case 1 \
  --base-url http://localhost:8090 \
  --output-dir /tmp/writer-perf/P01/case_1
```

## 常用参数

`full_run.sh` 支持：

| 参数 | 作用 |
|---|---|
| `--mode perf\|func\|both` | 选择套件；默认 `perf` |
| `--cases 1,2` | 只运行性能场景中的指定 case |
| `--run-id YYYYMMDD_HHMMSS` | 向指定批次追加新场景；已有场景目录时拒绝覆盖 |
| `--base-url URL` | 覆盖被测服务地址 |
| `--recipe text_intent\|explicit_mention` | 选择 API 路由信号 |
| `--no-ui` | 功能测试改走 API 通道 |
| `--no-trace` | 不获取 trace |
| `--no-checks` | 不执行功能机械检查 |
| `--no-stats` | 不计算性能统计 |
| `--venv PATH` | 指定 Python 解释器 |

单用例超时通过 `TIMEOUT_S` 设置，默认 600 秒：

```bash
TIMEOUT_S=900 bash tests/e2e/writer-test/scripts/full_run.sh --mode func C03
```

## 前端局部编辑与写回测试

`frontend/tests/e2e/writer_effect_ui.spec.ts` 针对已存在的长文会话执行四个
Playwright 用例：Markdown/IR 选区改写、手动编辑后写回、Markdown 写回飞书。
必须提供对应会话 ID；缺失时用例会被跳过。

```bash
cd frontend
AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID=<id> \
AI_WRITER_IR_EFFECT_CONVERSATION_ID=<id> \
AI_WRITER_WRITE_BACK_CONVERSATION_ID=<id> \
AI_WRITER_MARKDOWN_WRITE_BACK_CONVERSATION_ID=<id> \
pnpm test:writer-effects
```

这些 ID 必须分别指向与用例表示形式和 provider 匹配的会话。测试不会用“最近
会话”兜底。

## 输出位置

`full_run.sh` 输出到：

```text
reports/
  func/<timestamp>/<scenario>/case_1/
  perf/<timestamp>/<scenario>/case_N/
  ui/<run-id>/
```

功能单用例主要产物：

```text
run_N.json        状态、会话、耗时和 mechanical_verdict
trace.json        归一化 trace（启用时）
final.md          最终文档
evidence.json     expected、facts、checks、SSE、重试等结构化证据
report.md         机械结果与待填写的业务结论
_ui_bridge/       UI 模式录屏（成功或失败均保留）
```

IR 场景按需额外保留 `draft_document.json` 与 `export_document.md`。Runner 的
重复 stdout、空 stderr 和桥接临时 prompt 会自动清理；非空 stderr 保留用于诊断。

性能单用例额外产生 `document_stats.json`；场景目录产生聚合后的
`stats.json`、`stats.md`。失败用例保留运行记录，但不进入成功样本的性能聚合。

### 当前性能统计口径

列顺序保持与飞书“性能测试 / 全数据”表一致：批次、场景、阶段、总耗时、
LLM 推理轮数、LLM 累计耗时、Agent 工具调用次数、工具实际执行时间、输入
token/字符、输出 token/字符、draft 章节数、生成字数、修改字数、键。

- 阶段仅统计对应 `advance_step` 的完整执行子树（`advance_step_execution_tree`），
  包含步骤内部 Agent 决策、准备、workspace 执行和收尾。workspace 父链只用于识别步骤。
  步骤外主 Agent 的发起决策、路由和协调只计全流程，不延长阶段起点。
  不使用前端状态、相邻阶段时间窗、旧函数名映射或“步骤＋外部决策”口径兜底。
- 发起决策的串行轮次关联仅用于失败尝试剔除；同一 Agent、同一 trace、该轮唯一
  目标工具、无跨轮重叠，已有输出还须匹配工具名和 step_ids。成功步骤的外部决策
  无需归属阶段。失败/先前尝试的决策无法可靠关联时记录 `unresolved_dispatches`
  并阻止正式导出，不能猜测应扣除的调用。步骤内 workspace 重试保留成功发起决策。
- 远程子 Agent 仍要求 workflow session、subagent 标签和跨 trace 的唯一包围关系，
  关联记录在 `step_links`；决策关联记录在 `decision_links`，原始 observation 不变。
  `unassigned_llm_ids` 列出终态边界内未归属阶段的正常 LLM（包括前置路由）。
- 阶段 `full` 保留全部尝试，`normal` 只保留最后且成功的尝试，`abnormal` 记录其余
  调用。同一步内重试 workspace 时，从明确发起最后 workspace 的决策开始保留；
  先前尝试的决策也随尝试排除。末次失败不回退选择旧成功结果；可用的后端
  attempt_results 终态优先于 workspace 表面成功。失败样本不参与正常指标均值。
- 阶段墙钟为业务尝试区间并集：起点为 advance_step 开始（步骤内重试取成功重试的内部决策），终点包含执行收尾和晚结束的
  后代调用。正常墙钟扣除被排除调用独占的时间；失败并行调用与保留工作重叠的
  时间不能重复扣减。LLM 耗时逐调用累加，可以超过墙钟。工具次数仅计 Agent 直接
  调用的业务工具；工具时间扣除后代 LLM 区间并集，不减去并行 LLM 的时长之和。
- `full_link` 保留所有尝试和尾迹。`clean_full` 在 trace 起点到最后步骤返回的
  边界内保留正常调用，包括没有阶段归属的主 Agent 调用，排除先前尝试、失败前缀
  和 ERROR 子树；失败后代即使超出父 span，也参与排除。墙钟只扣除失败区间中
  不与保留工作重叠的部分。它是排除失败后的派生 trace 耗时，不是 runner 总墙钟，
  不要求等于阶段之和；批次 runner 耗时与成功链路耗时不得互相替换。
- 当前 LazyLLM 动态 supplier 的 token 为模型实例累计计数。采集记录
  `metadata.llm_usage_semantics=cumulative_entity`，分析器先按 traceId / entity.id
  及时间排序差分，再过滤失败尝试，保留 `usage_corrections` 审计记录，不修改原始
  observation。此口径要求完整采集本次新建模型实例的调用；不能用于从中途开始
  截取或跨运行复用模型的 trace。重叠调用、计数回退或错误后基线未知时直接报错。
  若部署已改为逐调用 usage，显式设置 `E2E_LLM_USAGE_SEMANTICS=per_call`；
  不根据数值递增猜口径。未声明口径的外部 trace 按标准逐调用 usage 读取。
- 字符数累计 trace 中已采集的原始 IO 载荷长度，截断片段（含采集标记）也按现存文本计入；
  未采集载荷不贡献字符数，不因缺少部分调用而清空整组。该值表示已采集字符累计，
  不代表完整请求/响应长度，也不从 token 换算；原有列名保持不变。章节数优先使用本次 Core 槽位的 `draft_blocks` 索引。

Langfuse 和 local trace 使用同一归一化结构，按本次会话/工作流标识及运行时间范围
关联，并按 span 开始时间筛选本次运行窗口内的调用；即使属于同一会话，在 runner 记录的结束时间之后才开始的 span（如异步 Memory Review）也不计入本次统计，因此统计不包含这部分 token 和耗时。采集会等待 span 结束、usage 到齐，且内容连续三次稳定后再统计；不用提示词
相似度或文件修改时间猜测归属。单例也输出 `stats.json` / `stats.md`。
启用采集/统计时，成稿缺失、trace 获取失败、统计无效均返回非零退出码，批量聚合
异常也不再忽略。flat 分支不在本性能套件覆盖范围内。

### 完整 LLM 诊断采集（不改变统计口径）

算法服务配置 `LAZYMIND_LLM_TRACE_DIAGNOSTICS=true`、
`LAZYMIND_LLM_TRACE_PAYLOAD_MAX_CHARS=0`，并重建对应容器后启用。
Compose 已透传这两个变量；部署是否开启应核验容器实际配置，不能仅看本地 `.env`。
完整说明见 [LLM 诊断采集](../../../algorithm/LLM_TRACE_DIAGNOSTICS.md)。

- 新数据位于 LLM span 的 `lazyllm.diagnostics.llm` 属性，随现有 Langfuse/local
  后端导出。保留最终请求中的模型输入字段、成功调用的格式化输出、完整性标记及响应计时。
- 不预计算字段、消息或工具的分项字符数；从完整请求 JSON 离线分析组成和字符比例。
  分项字符比例不等于精确 token 比例，token 仍按供应商 usage 语义处理。
- `0` 仅表示本地不截断；远端回读后仍须核对正文字符数与 SHA-256。
  诊断开关不能恢复历史缺失正文，也不能证明远端一定保存完整。
- 当前分析器的原有 16 列仍读取既有 IO 字段，不会自动改用诊断正文或把两份载荷相加。
  完整正文用于补充归因，不能把已有“累计已采集字符”列解释为完整模型输入输出总量。
- 阶段墙钟仍由 span 边界和失败区间计算；新增响应计时只用于解释等待/生成耗时。
  HTTP 逐次重试记录不等于 Writer 业务步骤重试，不另加为阶段 LLM 调用次数。

性能 Runner 在回读 trace 后默认执行完整诊断门槛（`--no-trace` 时跳过）。
`diagnostics_audit.json` 记录模型调用覆盖率、请求/输出字符数与 SHA-256 校验、
HTTP 尝试和响应计时检查。模型候选同时检查 name、GENERATION、语义和 LLM/CHAT/VLM
配置类型，不能仅以 GENERATION 子集声称采集完整。非流式 chunk 计时、
不支持解析器的首语义块计时允许为空；成功尝试仍须有响应头和总耗时。

调用分类不正确、诊断缺失或正文/计时校验失败时，Runner 保留原始证据和
`run_N.json`，设置 `collection_validation.status=BLOCKED`，不计算统计，返回 2。
`full_run.sh` 遇到性能 Runner 的退出码 2 会立即停止后续 case/场景，且不复制
该 trace 进入场景聚合。trace 获取失败同样阻断。独立重算也拒绝未正确分类的模型调用，
不会把普通 SPAN 静默漏出统计。此门槛不能修复服务端分类或恢复历史缺失正文。

### 性能统计核验

**运行前确认口径。** `E2E_LLM_USAGE_SEMANTICS` 支持 `cumulative_entity` 与
`per_call`，采集默认值对应当前累计 usage 实现，但默认值不等于部署证据。
升级 LazyLLM、模型路由或 tracing 后，检查实际运行版本的 usage 写入和清理路径，
不能只看本地 checkout。当前需关注 supplier `_record_usage`、动态路由的
`supplier.forward` 以及 trace hook 读取 usage 的位置：直接调用 `forward` 是否绕过
`__call__` 的用量清理，会改变每个 span 的 token 含义。按确认结果显式设置环境变量，
采集后检查 `trace.json.metadata.llm_usage_semantics`。历史 trace 无此字段时，分析器
默认按单次 usage 读取；重算前必须独立核实，不能把默认行为当作历史口径证明。

**从原始 trace 独立核验，再检查表格写入。**

- 抽查重复使用的模型实例（主 Agent 和阶段子 Agent 都要覆盖），核对
  `traceId`、`lazyllm.entity.id`、调用顺序和原始 usage。仅数值递增不能证明累计。
  完整、零起点且无缺失的累计序列，差分后的总量应等于最后一次累计值；逐调用
  usage 则直接求和。输入与输出分别核对，不对字符数或耗时做 token 式差分。
- 先对包含失败尝试的完整序列差分，再按业务调用归属选取正常调用。例如两次累计输入
  为 100、160，第一次属于失败尝试，则第二次正常调用只计 60，不能计 160。
  缺少起点、复用跨运行实例、同一实例并发、计数回退或错误后基线未知时，停止
  正常指标上报并说明缺失证据，不推算或填零。
- 核对正常调用集合：阶段之间的主 Agent 决策应进入 `clean_full`；明确失败尝试
  才排除。没有失败的样本，在相同时间边界内应保留全部正常调用。`full_link`
  包含失败及尾迹，不能无条件要求它与 `clean_full` 相等。
- 墙钟扣除排除区间的并集；LLM 累计耗时允许因并行而大于墙钟。字符按已采集载荷累计
  （包括截断片段，缺失部分不计），不要由 token 换算。核验各阶段、全流程及导出行，并检查
  `usage_corrections`，不能仅以“表格与 stats.json 一致”宣称统计正确。
- 比较旧批次时先核对阶段归属、usage 语义、失败过滤和时间边界。口径不一致时
  用原始 trace 做同口径分析，并明确区分历史表值与重算值；未获要求不覆盖旧表。

## 维护用例

- 功能场景只改 `writer_func_cases.yaml`；性能场景只改
  `writer_perf_cases.yaml`。
- 公共执行约束只改 `exec_constraints.yaml`，不要复制到每条提示词。
- 附件统一放在 `fixtures/func` 或 `fixtures/perf`，由 YAML 引用。
- 飞书基线变化时，同步更新 YAML `feishu_docs` 中的
  `history_version_id` 和 `baseline_revision_id`。
- `tests/e2e_old/` 是只读参考，不作为当前用例来源。

## 结果上报

功能结果写入飞书 Base 时，测试表判决应依据本次运行证据和对应专项测试项，
不能直接复制 `run_N.json.mechanical_verdict`。C01 分为“流式输出”和“路由/工具链”
两行；C*、I*、X* 分别对应路由/工具链、图片能力、交叉引用。详细边界见架构文档
“判决模型”一节。

性能数据应使用 `analyze_perf.sheet_rows(stats, batch_id)` 生成导出行：阶段使用
`normal`，全流程使用 `clean_full`；末次仍失败的用例不导出正常链路数据。目标
工作簿和字段结构应先通过 `lark-cli` 回读确认，不猜测 sheet、表或字段 ID。

重算后同步更新各场景 `stats.json` / `stats.md`、批次 `sheet_rows.json` /
`summary.md` 及相关分析说明。上报时检查「测试批次」「全数据」及「对比」的批次
选项、数据范围和公式；保持用户的基准批次选择，逐场景核对指标与变化率，验证后
恢复用户选中的场景。只保留最终报告、原始运行证据和简短上报说明，默认不另存旧版报告
备份、重复 trace、整份上传载荷或多份样式回读文件；用户明确要求历史备份时例外，
从可验证来源原样提取并记录来源和校验值，历史目录不得作为重算输入。

## 测试脚本自测

```bash
bash tests/e2e/writer-test/scripts/self_check.sh
```

统一入口只保留三项必要检查：Python 分析器/Runner 单测、Node UI 桥接单测、
TypeScript 编译。Node 单测会实际启动 Playwright Chromium，因此不再重复执行
仅列举用例的 `playwright --list`。

## 独立平台回读（无需先定义用例）

以下入口不调用 LazyMind 读入实现，也不写入平台。Notion 使用已登录的 `ntn`，
GitHub 使用已登录的 `gh`，Obsidian 直接读取指定 Vault 中的 Markdown 文件。
在仓库根目录运行（输出参数放在平台名称之前）：

```bash
PYTHONPATH=tests/e2e .venv/bin/python -m shared.readback \
  --output /tmp/notion-readback.json notion '<页面URL或ID>'

PYTHONPATH=tests/e2e .venv/bin/python -m shared.readback \
  --output /tmp/github-readback.json github \
  --repo owner/repo --path docs/note.md --ref feature/test

PYTHONPATH=tests/e2e .venv/bin/python -m shared.readback \
  --output /tmp/obsidian-readback.json obsidian \
  --vault '/absolute/path/to/Vault' --path '笔记.md'
```

输出路径已存在时拒绝覆盖。JSON 包含 provider、document_id、revision_id、content、
title、url 和 raw。Notion 的 content 是供检查的纯文本，完整格式与图片证据在 raw
的块树中（含递归子块和全部分页）；导出证据会去掉识别到的签名 URL 参数。
GitHub 必须明确 ref，先解析到提交 SHA 再读取文件，仅支持 github.com 普通仓库
的 UTF-8 文件，不支持 Wiki 或 Contents API 不提供 Base64 正文的大文件。
Obsidian 以 SHA-256 作为内容版本，拒绝路径或符号链接逃出 Vault。

这些模块只负责回读，不做用例判决、基线恢复、图片下载或附件存在性检查。
图片引用可从 Notion raw 块或 GitHub/Obsidian 原文提取，实际图片显示仍需另外验证。
远端 CLI 的总请求预算默认为 180 秒，可在平台名称前传 `--timeout` 修改。
若沙箱无法访问系统钥匙串，应在获准的宿主环境执行，不能将认证失败视为产品失败。

### 微信公众号草稿回读及授权来源

```bash
PYTHONPATH=tests/e2e .venv/bin/python -m shared.readback \
  --output /tmp/wechat-readback.json wechat --media-id '<草稿media_id>' --article-index 0
```

微信独立模块直接请求官方 `draft/get`，不调用 LazyMind provider。
凭据从进程环境读取：优先 `LAZYMIND_E2E_WECHAT_ACCESS_TOKEN`；未提供时使用
`LAZYMIND_E2E_WECHAT_APP_ID` 和 `LAZYMIND_E2E_WECHAT_APP_SECRET` 请求
`stable_token`（`force_refresh=false`）。统一命令入口会自动读取仓库根目录 `.env` 中这三个微信测试变量（使用 python-dotenv），
已有进程环境变量优先；其他 `.env` 配置不会注入环境。直接调用 Python 回读函数时仍由调用方提供环境。
不从产品数据库抽取密钥、不缓存或输出 token。已有 token 失效时明确报错，不自动刷新重试。
需要对应公众号的草稿接口权限，并满足其 IP 白名单要求。

article-index 从 0 开始；输出正文 HTML、所选文章元数据、图片引用和文章内容哈希。
哈希不是微信原生版本号。`content_source_url` 仅是“阅读原文”链接，不作为预览链接，
顶层 url 暂留空；用 media_id 在公众号后台核查。脚本不发布、不修改草稿。

当前独立回读授权来源：飞书是 `lark-cli` 用户登录；Notion 是 `ntn` 的登录凭据
（CLI 也支持环境 token）；GitHub 是 `gh` 的登录凭据（CLI 也支持环境 token）；
Obsidian 是执行进程对指定 Vault 的文件读取权限；微信是上述测试环境变量。
这些授权与 LazyMind 内部的云文档授权互相独立，CLI 登录成功不等于所有资源都有权限。
