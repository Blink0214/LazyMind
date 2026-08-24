---
name: writer-test
description: >
  AI Writer 测试统一栈：功能测试（C01–C07 / I01–I02 / X01–X03，UI 或 no-UI，
  混合判定）+ 性能测试（P01–P05 × 5，纯 API 三阶段互斥统计）。工作流三个阶段
  已封装为 writer_prepare_workspace / writer_outline_workspace /
  writer_draft_workspace，trace 只有 workspace 级 span，槽位与修改计划以 core
  会话为准。
---

# Writer 测试（统一栈）

## 目标与边界

* 功能测试：`cases/writer_func_cases.yaml` 的 C01–C07 / I01–I02 / X01–X03；
* 性能测试：`cases/writer_perf_cases.yaml` 的 P01–P05（每场景 5 个 case，API-only）；

## 新业务逻辑对测试的影响（必须理解）

1. 每个阶段是一个 workspace 顶层工具，规划/媒体/草稿/修订等内部函数不再作为
   独立 span 出现在 trace 中；工具断言只针对三个 workspace 工具。
2. 槽位存在性/扩展名/stage/revision 与 `document_modify_plan`（modify_types）
   由 runner 从 core 会话补全。
3. 路由由 `writer_draft_workspace` 输出的 `operation`
   （generate/rewrite/revise）与 `writer_prepare_workspace` 是否绑定源文档决定。
4. 编号在被引用对象 materialize 时重算写回；正文引用文字不会自动更新（产品
   现状）。X03 按“应自动更新”期望断言，当前预期 FAIL。

## 脚本与职责

`tests/e2e/shared/`：`api.py`（认证/chat/等待/load_slot）、
`observability.py`（trace/SSE）、`feishu.py`（lark-cli 基线重置）、
`case_loader.py`（YAML 场景与飞书占位符）。

`tests/e2e/writer-test/`：

| 脚本 | 职责 |
|---|---|
| `runners/func_run.py` | 功能单场景（UI/API）+ 取证 + 会话补全 + 机械检查 |
| `runners/perf_run.py` | 性能单 case：API 执行 + trace + 三阶段统计 |
| `analyzers/analyze_common.py` | trace 事实抽取（workspace 路由/步骤/工具/写回/provider/models） |
| `analyzers/analyze_func.py` | 媒体/交叉引用/编号/SSE/飞书/可编辑性机械检查 |
| `analyzers/analyze_perf.py` | 三阶段互斥性能统计与 stats 报告渲染 |
| `scripts/full_run.sh` | 一键编排（默认 perf；`--mode func` 为功能） |
| `frontend/tests/e2e/writer_bridge.mjs` | Playwright 桥接（阶段面板跟随、自愈重试等待） |

---

# 一、功能测试（func）

## 怎么跑（func）

```bash
# 全量功能场景（C01–C07、I01–I02、X01–X03），默认 Playwright UI
bash tests/e2e/writer-test/scripts/full_run.sh --mode func

# 单场景（UI / 无 UI）
bash tests/e2e/writer-test/scripts/full_run.sh --mode func C03
bash tests/e2e/writer-test/scripts/full_run.sh --mode func --no-ui C01

# 直接调用（输出目录按 full_run.sh 约定：reports/func/<时间戳>/<场景>/case_1/）
python3 tests/e2e/writer-test/runners/func_run.py \
  --cases-root tests/e2e/writer-test/cases --scenario X01 \
  --base-url http://localhost:8090 \
  --output-dir tests/e2e/writer-test/reports/func/$(date +%Y%m%d_%H%M%S)/X01/case_1
```

执行规则（Agent 必须遵守）：

* 场景串行；任务状态按 **20 秒**轮询；
* 单用例因异常超过 **10 分钟**未完成：停止并保留现场、记录原因、继续下一用例；
* 面板 failed 后等待 **30 秒**观察会话自愈重试，完成后分析重试原因与结果；
* 只用本次运行生成的 trace/产物；不泄露任何密钥/凭据；不改业务代码、不改写结果。

## 产物（func）

`case_1/` 下：

```text
run_N.json       运行信封（状态/耗时/会话/checks/retry_count/models）
trace.json       归一化 trace
final.md         最终文档（IR 场景另有 evidence/draft_document.json）
checks.json      机械检查结果
evidence/        expected.json / facts.json / draft_document.* /
                 resolved_media_assets.json / retries.json / sse.json
report.md        机械检查表 + LLM 待填两节
_ui_bridge/      录屏/截图（--ui）
```

full_run.sh 输出根目录：`reports/func/<时间戳>/<场景>/case_1/`。

`evidence/retries.json`：重试事件、trace ERROR span、会话步骤状态，是
“重试原因 + 重试后通过与否”分析的主要证据。

## 机械检查项（func）

* 基础：`route` / `tools.required` / `tools.forbidden`（rewrite/revise 禁
  writer_outline_workspace）/ `steps` / `slot.*` / `write_back` /
  `provider` / `feishu.revision`；
* 产物：`media` / `media.display`（仅 UI）/ `final.ui_editable` /
  `final.new_revision`；
* 交叉引用与编号：`final.cross_references`、`final.reference_integrity`、
  `final.numbering`、`final.reference_number_consistency`；
* SSE：`sse.protocol` / `sse.streaming` / `sse.reconstruction`（UI 由桥接
  XHR 实时捕获；--no-ui 为 best-effort）。

## 大模型层（Agent 职责）

1. 读取 `evidence/expected.json` 与 `checks.json`；
2. 对每个 FAIL/WARN：打开 `facts.json` / `trace.json` / `draft_document.*` /
   `retries.json` / `resolved_media_assets.json` / 飞书 revision / 录屏，建立
   证据链，区分**可恢复偶发异常**与**真实缺陷**；证据不足标“证据不足”，不得
   臆测；
3. 核对机械检查是否合理（workspace 路由推断依赖的信号是否真实）；
4. 填写 `report.md` 的“异常与说明（LLM 分析）”与“最终结论（LLM 判定）”，
   结论为 `PASS` / `PASS_WITH_RETRY` / `FAIL` 并给出依据；
5. 只用本次运行持久化产物，缺失数据标 WARN。

## 报告格式（func）

```text
# 功能测试报告：<conversation_id>
- mode / session / writer_status / elapsed / trace / facts / checks /
  evidence / feishu revision / retries / models
## 机械检查（脚本比对）
汇总：PASS=x / FAIL=y / WARN=z
| Status | Check | Detail |
## 异常与说明（LLM 分析）   <- Agent 填写
## 最终结论（LLM 判定）     <- PASS / PASS_WITH_RETRY / FAIL + 依据
```

## 功能测试结果写入飞书数据表（Base）

目标为功能测试 wiki 内嵌多维表格：
`https://sensetime.feishu.cn/wiki/SpmNwUEpfiaRSQkXFKYcnMzCnzb`。
操作走 `lark-cli base`（lark-base skill）：先 `+url-resolve` 拿真实 `base_token`，
不要用 wiki token / 完整 URL 直接当 base token；表名、字段名、选项名一律以
`+table-list` / `+field-list` 返回为准；写记录前先读 lark-base 的
`lark-base-record-upsert.md` / `lark-base-record-batch-create.md` /
`lark-base-cell-value.md`。

涉及两张表：

| 表 | table_id | 用途 |
|---|---|---|
| 测试批次 | tblY0RndBNDLHxst | 每个批次一行（FTxxxx） |
| 功能测试 | tblYbPTv0l5ZSr8a | 每个用例一行结果（含未测试） |

### 1. 写测试批次表

新增批次行，只写存储字段：

- `测试标签` = FTxxxx（顺延既有批次编号，如 FT0004）；
- `LazyMind Commit` / `LazyLLM Commit` = 本次运行 HEAD（如 6a569578 / 7b471375）；
- `模型/配置` = 实际模型名（如 MiniMax-M2.7-highspeed）；
- `测试时间` 为 created_at 自动字段，不写。

### 2. 写功能测试表

按“一用例一行（C01 拆 流式输出 + 路由/工具链 两行，与既有批次一致）”写入：

- 字段：`测试批次` / `用例ID` / `测试场景` / `测试项` / `测试结果` / `备注` / `失败描述`；
- 用例映射：C01=0→1、C02=Expand MD、C03=Expand IR、C04=Rewrite MD、C05=Rewrite IR、
  C06=Revise MD、C07=Revise IR、I01=Figure MD、I02=Figure IR、X01=Cross Ref MD、
  X02=Cross Ref IR、X03=Cross Ref Update MD；
- 测试项：C* = 路由/工具链（C01 另加 流式输出）、I* = 图片能力、X* = 交叉引用；
- 测试结果用真实选项名：`待测试` / `PASS` / `PASS_WITH_RETRY` / `FAIL` / `ERROR` /
  `BLOCKED`（“未测试”写作 `待测试`，不要新增选项）；
- 未执行用例记 `待测试`；**PASS 行不写备注**；FAIL/ERROR 行写 `备注`（原因摘要）+
  `失败描述`（证据）；
- `测试日期` / `更新日期` 为自动字段，`截图/录屏` 为附件字段，均不写。

### 3. 新建批次视图并校准列顺序

- `+view-create` 建 grid 视图，命名 FTxxxx；
- `+view-set-filter` 筛选 `测试批次 == FTxxxx`（与既有 FT0001..FT0003 视图一致）；
- 新建视图的字段排列可能乱序，必须用 `+view-set-visible-fields` 对齐源数据表默认
  视图顺序：`测试批次 / 用例ID / 测试场景 / 测试日期 / 更新日期 / 测试项 / 测试结果 /
  失败描述 / 备注 / 截图/录屏`。

### 4. 校验

写入后 `+record-list`（按 `测试批次` 过滤核对行数与结果）和
`+view-get-visible-fields` 回读校验；测试标签 / 用例 / 结果必须可追溯到本次运行产物
（run_N.json / checks.json / trace.json），不泄露密钥与凭据。

---

# 二、性能测试（perf）

## 怎么跑（perf）

```bash
# 默认 perf：全部 P01–P05（每场景 5 个 case）
bash tests/e2e/writer-test/scripts/full_run.sh

# 指定场景 / 指定 case
bash tests/e2e/writer-test/scripts/full_run.sh P03 --cases 1,2

# 直接调用（输出目录按 full_run.sh 约定：reports/perf/<时间戳>/<场景>/case_N/）
python3 tests/e2e/writer-test/runners/perf_run.py \
  --cases-root tests/e2e/writer-test/cases --scenario P01 --case 1 \
  --base-url http://localhost:8090 \
  --output-dir tests/e2e/writer-test/reports/perf/$(date +%Y%m%d_%H%M%S)/P01/case_1
```

性能模式为**纯后端 API**（无浏览器）。判定：`writer_status == completed`
才算成功；统计带 `present/n` 覆盖率，缺失按 0 计并在报告提示。

执行约束（触发次数/步骤推进/兜底禁止/标题层级/重试上限等）统一维护在
`cases/exec_constraints.yaml`（`write` / `revise` 两套模板），由 case loader
在测试开始时注入到提示词末尾；用例 YAML 的 `text` 不再内嵌约束段落，只需声明
`constraints: write|revise`。报错纠正重试上限为 **3 次**。

## 产物（perf）

每个 case 产出 `case_N/run_N.json`、`trace.json`、`final.md`；场景级聚合产出
`stats.json` / `stats.md`。
full_run.sh 输出根目录：`reports/perf/<时间戳>/<场景>/case_N/`
（`--mode both` 时功能与性能共用同一时间戳，分别落在 func / perf 子目录）。

## 统计口径（perf）

三阶段（prepare / outline / write_document）互斥统计，由
`analyzers/analyze_perf.py` 计算：

* 阶段时间窗优先按 `advance_step` 的 step_id 划分（新工作流 step_id 在 trace
  元数据 `lazyllm.io.output`）；旧 trace 无边界时按 workspace 函数名回退；
* 每个场景的 `stats.md` 统一为**两张同维度大表**（每个用例同样如此）：
  1. **完整过程统计主表**：阶段窗口内全部观测（所有尝试、触发前探测、阶段间决策、
     终态后尾迹），时间上包含异常/尾迹部分；
  2. **异常/尾迹时间统计**：与完整过程表同维度，仅列非正常链路部分（非最后一次
     尝试的重试、终态后尾迹等）；
  3. 另附**异常来源说明**（ERROR span、调用次数/重试次数、末次是否成功、尾迹窗口）。
  正常链路（每个阶段/工具调用的**最后一次成功**）见 `stats.json` 的
  `phases.*.normal`，供导出使用；
* write_document 章节数取自 `writer_draft_workspace` 输出的
  `draft_section_count`（旧 trace 回退按草稿 span 计数）；**修订场景无章节数上报
  （输出 `draft_section_count: null`），章节数按 “—” 计**；
* “全流程”阶段：每个场景另生成一行阶段=“全流程”（键 `批次ID|场景|全流程`），
  墙钟 / LLM / token 取 trace 全链路（`full_link`），工具次数/工具时间按各阶段
  正常链路合计（旧统计无 normal 时回退完整过程），章节数与生成/修改字数取
  write_document 阶段值；由 `analyze_perf.sheet_rows(..., with_full_flow=True)`
  自动生成，失败场景不生成全流程行；
* 生成/修改字数取自 `shared/document_metrics.document_stats`（runner 在
  `case_N/document_stats.json` 落盘并并入 `stats.json` 的 write_document 阶段），
  与数据表“生成文章字数/修改文章字数”列对齐；失败用例无 final 时按 “—” 计；
* `model_info`：供应商 + 具体模型名（`resolved_prompt.model`）；
* 异常附表：ERROR span、重复 Writer 步骤、失败 patch（重复工具的次数即总尝试数，
  是否恢复以最后一次尝试的 level 与 `writer_status` 为准）。

性能用例不需要 LLM 判定两节；Agent 按 `stats.md` 与异常附表给结论即可。

## 性能数据导出到飞书数据表

目标：`https://sensetime.feishu.cn/wiki/G6uywUIWui9yEWkvWztcQStKnZ2`（wiki 内嵌
电子表格），含 全数据（EPIknh）/ 对比 / 测试批次 三张子表；先 `+workbook-info`
拿 sheet_id，勿猜。

场景统一使用 **P01–P05**（0-1 写作 / 基于 Markdown 的大纲写作 / 基于飞书的大纲写作 /
基于 Markdown 的全文修改 / 基于飞书的全文修改）；旧场景目录命名已废弃，不再需要映射。

统计维度对齐（全数据列 ↔ stats.json / document_stats.json）：

| 全数据列 | 来源 |
|---|---|
| 测试批次ID | PT0001 / PT0002 … |
| 测试场景 | P01–P05 |
| 阶段 | prepare / outline / write_document / 全流程（修订场景无 outline 行） |
| 总耗时（s） | `phases.*.wall` |
| LLM 推理轮数 | `llm_count` |
| LLM 累计耗时（s） | `llm_latency` |
| Agent 工具调用次数 | `tool_count` |
| 工具实际执行时间（s） | `tool_time` |
| LLM 累计输入 token | `input` |
| LLM 累计输入字符 | `input_chars` |
| LLM 累计输出 token | `output` |
| LLM 累计输出字符 | `output_chars` |
| draft 章节数 | `chapters`（非成稿阶段为 “—”；修订场景成稿与全流程行均为 “—”） |
| 生成文章字数 | `document_stats.final.visible_characters` |
| 修改文章字数 | `document_stats.revision.changed_final_characters`（修订场景） |
| 键 | `批次ID|P0X|阶段`（对比表 INDEX/MATCH 依据） |

导出规则：写入数据表时**去掉异常/尾迹时间，只取每个阶段/工具调用最后一次成功
（normal）的时间**；末次仍失败则本用例判失败，不导出正常链路数据，记录到最终
分析报告与测试批次表备注。

导出步骤（lark-cli sheets，外网需授权）：

1. 用 `analyze_perf.sheet_rows(stats, batch_id)` 从各场景 `stats.json` 生成
   “全数据”行（只含 normal=最后一次成功尝试；失败用例整例跳过；成功场景自动
   追加一行阶段=“全流程”），失败清单写入最终报告；
2. `+csv-get` 读全数据确认真实末行；键列统一为 `批次ID|P0X|阶段`；
3. `+cells-set` 在空行追加新批次（数值列传数字并配 number_format
   `0.000` / `0.0` / `#,##0`，文本列 `@`，缺值 “—”），键为 `批次ID|P0X|阶段`；
4. `+dropdown-update --ranges '["全数据!A2:A200"]' --options '["PT0001","PT0002"]'`
   （range 里的 sheet 前缀**不要**加单引号）；对比表 B2:B3 同；
5. 对比表：基准批次=PT0001、本次批次=PT0002、场景=P01..P05，公式自动
   INDEX/MATCH 出 基准 / 本次 / 变化率；基准/本次公式已加 `IFERROR(...,"-")`
   兜底——无对应阶段的场景（如 P04/P05 的 outline、失败场景的本次批次）直接显示
   “-”，不显示 #N/A；阶段行号映射为
   `IF(ROW()<=15,"prepare",IF(ROW()<=24,"outline",IF(ROW()<=34,"write_document","全流程")))`，
   即 prepare 7–15 / outline 16–24 / write_document 25–34 / 全流程 35–46；
   全流程块为 12 项指标（含章节/字数），阶段标签 A35:A46 已合并；
   变化率列（E7:E46）已配置条件格式（负值绿 / 正值红），新批次数据写入后自动着色，
   无需重建规则；
6. 测试批次表：追加批次行（测试时间 / LazyMind、LazyLLM commit / 模型 /
   负责人 / 场景覆盖 / 总墙钟 / 备注）。**模型/配置写具体模型名**
   （`stats.json` 的 `model_info.request_model` 中除 `llm` 外的值，如
   `MiniMax-M2.7-highspeed`，供应商可并列如 `minimax / MiniMax-M2.7-highspeed`）；
   总墙钟 = 各 case `run_N.json` 的 `elapsed_s` 合计；
7. 回读校验：全数据 2..末行逐行核对；对比 C7:D46 无 #N/A（切到每个场景抽查
   prepare / write_document / 全流程 各一格），完成后恢复场景=P01。

口径注意：

* 阶段统计为**两表制**：完整过程（含异常/尾迹）与异常/尾迹同维度并列；导出只取
  最后一次成功尝试（normal），失败用例（末次仍失败）整例不导出、写入最终报告；
* 失败用例（无 final.md）生成/修改字数为 “—”；
* PT0001 为旧脚本全链路口径，与正常链路口径对比“变化率”时有口径差异，需说明。
* “全流程”阶段口径：PT0001（旧脚本无 normal 子桶）按完整路径取值；
  PT0002 起按正常链路合计；失败场景不导出全流程行。

---

## 测试与开发

```bash
cd tests/e2e/writer-test && PYTHONPATH=../ \
  /Users/chensiyu2/Code/LazyMind/.venv/bin/python -m unittest discover
```

## 修改安全

* 飞书文档基线变化时重新提取 revision/history 后更新两个 YAML 的 `feishu_docs`；
* 执行约束提示词只改 `cases/exec_constraints.yaml`，不要在各用例 `text` 中内嵌；
* 只检查/修改当前任务涉及的文件，不还原、不暂存无关改动；
* 改测试脚本后必须跑通单元测试。
* 飞书导出相关维护点（改 `analyze_perf.py` / 表格结构时注意）：
  - `sheet_rows` 的“全流程”行与修订场景章节数 “—” 口径见上文统计与导出规则，
    改动需同步更新对比表公式行号映射（prepare 7–15 / outline 16–24 /
    write_document 25–34 / 全流程 35–46）与条件格式范围（E7:E46）；
  - 聚合口径变更后必须重跑单元测试（`tests/` 覆盖 normal 子桶、章节数、
    失败场景不导出等行为）。
