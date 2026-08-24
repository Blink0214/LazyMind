# AI Writer 测试（writer-test 统一栈）

> 新版 Writer 测试说明。旧版 `tests/e2e_old/writer-e2e/` 与
> `tests/e2e/writer-benchmark/` 为只读备份，仅参考思路，不再维护。
> Agent 侧契约见 [`SKILL.md`](SKILL.md)。功能与性能两套测试在本文件中并列说明。

## 1. 业务背景（为什么测试长这样）

- Writer 工作流的 prepare / outline / write_document 三个阶段已封装为三个顶层
  工具：`writer_prepare_workspace`、`writer_outline_workspace`、
  `writer_draft_workspace`；规划、媒体、草稿、修订等内部子步骤**不再作为独立
  工具出现在 trace 中**（trace 只有这 3 个 workspace 工具 + `advance_step` +
  `llm` / `image_generator` 等内部 span）。
- 槽位存在性 / 扩展名 / stage / revision，以及 `document_modify_plan`
  （modify_types），**以 core 会话为准**，runner 在分析阶段自动补全。
- 编号：被引用对象（标题/图注）的编号在导出/materialize 时重算写回；正文引用
  文字目前不会随目标编号自动更新（产品现状）。X03 按“应自动更新”的期望断言，
  当前预期 FAIL。

---

# 一、功能测试（func）

## 1.1 用例清单

`cases/writer_func_cases.yaml` 是功能场景注册表。场景一览：

| 场景 | 路由 | 表示 | 写回 | 关键断言 |
|---|---|---|---|---|
| C01 | create | Markdown | 0 | 从零成稿；不写回 |
| C02 | expand | Markdown | 0 | 按上传大纲扩写；不写回 |
| C03 | expand | 飞书 IR | replace×1 | 大纲源；写回一次且 revision 增加 |
| C04 | rewrite | Markdown | 0 | 重写上传全文；无 outline 步骤 |
| C05 | rewrite | 飞书 IR | replace×1 | 重写；写回一次且 revision 增加 |
| C06 | revise | Markdown | 0 | 组合修改（增/删/替/移）；**当前已知缺陷** |
| C07 | revise | 飞书 IR | publish×1 | 组合修改；发布 revision 且增加 |
| I01 | create | Markdown | 0 | ≥2 张配图 + 图注（不限定图片来源）；浏览器渲染 |
| I02 | expand | 飞书 IR | replace×1 | ≥2 张配图 + 图注（不限定图片来源）；写回 |
| X01 | create | Markdown | 0 | 章节交叉引用 + 表格引用；锚点/题注/编号完整 |
| X02 | expand | 飞书 IR | replace×1 | 配图引用 + 章节引用；编号完整 |
| X03 | revise | Markdown | 0 | 移动章节后编号自动更新 + 正文引用保留/更新 |

## 1.2 执行方式

```bash
# 全部功能场景（默认 Playwright UI）
bash tests/e2e/writer-test/scripts/full_run.sh --mode func

# 单场景（UI / 无 UI）
bash tests/e2e/writer-test/scripts/full_run.sh --mode func C03
bash tests/e2e/writer-test/scripts/full_run.sh --mode func --no-ui C01

# 直接调用 runner（输出目录可控）
PY=/Users/chensiyu2/Code/LazyMind/.venv/bin/python
$PY tests/e2e/writer-test/runners/func_run.py \
  --cases-root tests/e2e/writer-test/cases \
  --scenario X01 --base-url http://localhost:8090 --output-dir /tmp/writer-test/X01
```

full_run.sh 的功能输出统一放在 `reports/func/<时间戳>/<场景>/case_1/`。

### UI 与 no-UI 的差异

| 能力 | UI（Playwright） | no-UI（API） |
|---|---|---|
| 路由/工具/步骤/槽位/写回/媒体/交叉引用/编号检查 | ✅ | ✅ |
| 飞书基线重置/写回/修订号 | ✅ | ✅ |
| `media.display`（浏览器实际渲染 + 失败图检测） | ✅（渲染数 + 0 尺寸/加载失败图清单） | WARN（无法验证渲染） |
| 阶段面板跟随录屏、桥接 retry_events | ✅ | 无（重试分析靠 trace ERROR + 会话步骤） |
| SSE 协议/流式效果/重建检查 | ✅（桥接 XHR 实时捕获） | best-effort（task_id 可查时捕获） |

## 1.3 两种执行方式（通过 / 不通过）

“**通过**”= 交给 Agent（读 SKILL.md 自动执行并出报告）；“**不通过**”=
你自己在终端跑命令。

### 方式 A：通过 —— 交给 Agent

Agent 读取 SKILL.md 契约后自动完成：串行执行 → 20 秒轮询 → 10 分钟熔断 →
30 秒自愈重试等待 → 取证 → 填写 `report.md` 的“异常与说明 / 最终结论” →
汇总通过/不通过。

给 Agent 的提示词示例（全量回归）：

> 请串行运行 Writer 功能测试全部场景（C01–C07、I01–I02、X01–X03），使用前端
> Playwright 自动化（默认模式），任务状态按 20 秒间隔轮询；单用例因异常超过
> 10 分钟未完成时停止并保留现场、记录原因后继续下一用例；面板 failed 后等待
> 30 秒观察自愈重试。全部完成后，读取每个用例的 `run_N.json` / `checks.json` /
> `evidence/retries.json`，按 `tests/e2e/writer-test/SKILL.md` 填写各
> `report.md` 的“异常与说明”和“最终结论”两节，并在最终回复中给出每个用例
> 通过/不通过及依据。只使用本次运行生成的 trace 和产物，不得泄露任何密钥或
> 访问凭据。

### 方式 B：不通过 —— 终端直跑

```bash
$PY tests/e2e/writer-test/runners/func_run.py \
  --cases-root tests/e2e/writer-test/cases \
  --scenario X03 --output-dir /tmp/writer-test/X03
cat /tmp/writer-test/X03/case_1/run_N.json
cat /tmp/writer-test/X03/case_1/checks.json
cat /tmp/writer-test/X03/case_1/report.md
```

### 两者区别

| 维度 | 方式 A：通过 Agent | 方式 B：终端直跑 |
|---|---|---|
| 谁执行 | Agent 按 SKILL.md 契约自动执行 | 你自己敲命令 |
| 轮询/熔断/重试等待 | Agent 自动（20s / 10min / 30s） | 自己控制（full_run.sh 无内置熔断） |
| 机械检查 | runner 自动生成 | runner 自动生成（相同） |
| report.md 的 LLM 两节 | Agent 填写 | **留空**，需自己分析 |
| 最终通过/不通过汇总 | Agent 给出 | 自己读 checks.json |
| 适用场景 | 全量回归、缺陷复现、出报告 | 快速抽查、调试单场景 |

## 1.4 产物与证据（func）

每个用例产出 `case_1/`：

```text
run_N.json                      运行信封（状态/耗时/会话/checks/重试计数/models）
trace.json                      归一化 trace（Langfuse）
final.md                        最终文档（IR 场景另有 evidence/draft_document.json）
checks.json                     机械检查结果（PASS/FAIL/WARN + detail）
evidence/
  expected.json                 预期断言（来自 YAML）
  facts.json                    trace 事实（route/steps/tools/write_back/provider/models）
  draft_document.*              最终产物
  resolved_media_assets.json    媒体资产（媒体场景）
  retries.json                  重试事件 + trace ERROR span + 会话步骤状态
  sse.json                      SSE 流（UI 由桥接 XHR 捕获 / --no-ui best-effort）
report.md                       机械检查表 + LLM 待填两节
_ui_bridge/                     录屏/截图（--ui）
```

## 1.5 机械检查清单

| 检查 | 含义 |
|---|---|
| route | create / expand / rewrite / revise（来自 workspace 输出 operation + prepare 是否绑定源文档） |
| tools.required / forbidden | 3 个 workspace 工具出现；rewrite/revise 不得出现 writer_outline_workspace |
| steps | prepare / outline / write_document 实际顺序（来自 advance_step） |
| slot.* | 会话槽位存在性、扩展名、representation、stage、provider、revision |
| write_back | 写回次数与工具（workspace 输出 document_write_result 归因） |
| provider / feishu.revision | 飞书判定与 revision 严格递增 |
| media / media.display | 正文图片数、生成资产；UI 额外校验浏览器渲染且失败/异常图必须为 0 |
| final.ui_editable / new_revision | IR 可编辑、draft revision 新建 |
| final.cross_references | 正文引用数量与（可选）源引用保留 |
| final.reference_integrity | 章节/图/表/代码块锚点+题注+编号完整；引用全部命中真实目标（悬空列出） |
| final.numbering | 编号序列正确（章节层级、图/表/代码递增）；（require 时）不得缺失 |
| final.reference_number_consistency | 正文引用文字编号 == 目标当前编号 |

## 1.6 报告格式与 LLM 判定

```text
# 功能测试报告：<conversation_id>
- mode / session / writer_status / elapsed / trace / facts / checks /
  evidence / feishu revision / retries / models
## 机械检查（脚本比对）      ← 汇总 PASS=x / FAIL=y / WARN=z + 检查表
## 异常与说明（LLM 分析）    ← 方式 A 由 Agent 填；方式 B 留空
## 最终结论（LLM 判定）      ← PASS / PASS_WITH_RETRY / FAIL + 依据
```

机械检查 FAIL 不等于产品失败：需核对取证形态问题与真实缺陷，证据不足标
“证据不足”，不得臆测。

---

# 二、性能测试（perf）

## 2.1 用例清单

`cases/writer_perf_cases.yaml`：P01 从零写作 / P02 基于 Markdown 大纲 /
P03 基于飞书大纲 / P04 基于 Markdown 全文修改 / P05 基于飞书全文修改，
每场景 5 个 case（P01-1…P05-5），共 25 个。P03/P05 各 case 使用独立的
飞书文档（`feishu_docs` 注册表，跑前自动重置基线）。

执行约束提示词统一维护在 `cases/exec_constraints.yaml`（`write` / `revise`
两套模板），case loader 在测试开始时注入到提示词末尾；用例 YAML 只声明
`constraints: write|revise`，`text` 不再内嵌约束段落。报错纠正重试上限为
**3 次**。

## 2.2 执行方式

```bash
# 默认 perf：全部 P01–P05（每场景 5 个 case）
bash tests/e2e/writer-test/scripts/full_run.sh

# 指定场景 / 指定 case
bash tests/e2e/writer-test/scripts/full_run.sh P03 --cases 1,2

# 直接调用（单 case，输出可控）
python3 tests/e2e/writer-test/runners/perf_run.py \
  --cases-root tests/e2e/writer-test/cases \
  --scenario P01 --case 1 --base-url http://localhost:8090 \
  --output-dir /tmp/perf
```

性能模式为**纯后端 API**（无浏览器）。判定：`writer_status == completed`
才算成功。
full_run.sh 的性能输出统一放在 `reports/perf/<时间戳>/<场景>/case_N/`
（`--mode both` 时功能与性能共用同一个时间戳，分别落在 func / perf 子目录）。

## 2.3 统计口径与产物

每个 case 产出 `case_N/run_N.json`、`trace.json`、`final.md`；场景级聚合产出
`stats.json` / `stats.md`（`analyzers/analyze_perf.py`）。

三阶段（prepare / outline / write_document）互斥统计：

- 阶段时间窗优先按 `advance_step` 的 step_id 划分（新工作流 step_id 在 trace
  元数据 `lazyllm.io.output` 中）；旧 trace 无边界时按 workspace 函数名回退；
- 每阶段统计：墙钟、LLM 轮数/耗时/输入输出 token、工具调用数与执行耗时、
  字符载荷。阶段列按**正常链路（首次尝试）**统计；workspace 工具第 2+ 次调用
  （异常重试）单独列在“异常重试明细”（重试次数/墙钟/LLM/输入输出 token/最终
  是否成功），不参与阶段对比，`stats.json` 的 `retries` 数组为原始明细；
- write_document 章节数取自 `writer_draft_workspace` 输出的
  `draft_section_count`（旧 trace 回退按草稿 span 计数）；
- `model_info` 含供应商与具体模型名（`resolved_prompt.model`）；
- 异常附表：ERROR span、重复 Writer 步骤、失败 patch。

---

## 3. 环境准备（两套共用）

- 服务：`http://localhost:8090`（frontend / core / chat 容器），代码更新后需
  重建 frontend/core 并重启 chat。
- 飞书用例需要本机安装并登录 `lark-cli`；文档 URL 与基线在 YAML `feishu_docs`
  注册表，跑前自动重置，不需要环境变量。
- 本地 fixture：`fixtures/func/writer-outline.md`、`writer-full.md`、
  `writer-refs.md`（带编号标题与内部链接的修改用例源文档）。
- 认证：runner 自动登录；产物中不落任何凭据。

## 4. 修改安全

- `tests/e2e_old/writer-e2e/`、`tests/e2e/writer-benchmark/` 为只读备份，不改；
- 飞书文档基线变化时，重新提取 revision/history 后更新两个 YAML 的
  `feishu_docs`；
- 执行约束提示词只改 `cases/exec_constraints.yaml`，不要在各用例 `text` 中内嵌；
- 只检查/修改当前任务涉及的文件，不还原、不暂存无关改动；
- 测试脚本改动后必须跑通单元测试：

```bash
cd tests/e2e/writer-test && PYTHONPATH=../ \
  /Users/chensiyu2/Code/LazyMind/.venv/bin/python -m unittest discover
```
