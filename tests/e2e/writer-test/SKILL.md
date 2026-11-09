---
name: writer-test
description: >
  Run and assess LazyMind long-form AI Writer functional or performance E2E tests,
  including Playwright UI evidence, workflow traces, provider write-back, and reports.
---

# Writer E2E Agent 约束

本 skill 只覆盖长文写作，不执行 flat/短文写作测试。

开始前按任务需要阅读：

- [README.md](README.md)：环境、命令、参数、产物和结果上报方式；
- [架构与判决语义](e2e-test-architecture.md)：执行链路、证据来源、检查边界及判决模型。

## 执行约束

- 功能用例只从 `cases/writer_func_cases.yaml` 加载；性能用例只从 `cases/writer_perf_cases.yaml` 加载。
- 用户输入仅使用用例 YAML 的 text（解析资源占位符），不得自动追加 exec_constraints.yaml 执行约束。
- 使用 `scripts/full_run.sh` 编排多场景。涉及飞书文档的用例必须串行，避免共享
  provider 状态互相覆盖。
- Provider 基线恢复是 fail-fast preflight。恢复、回读或内容校验失败后不得发送
  Writer 请求，也不得把该次运行判为产品功能失败。
- UI 模式必须使用登录 token 解出的同一 `user_id`。会话 ID 必须来自本次
  `conversations:chat` SSE；URL 和 sessionStorage 仅用于交叉核对。新面板
  `session_id` 必须与该 conversation 的 Core session 一致。
- 单用例超时后停止该用例并保留已有产物，记录原因后再继续后续用例。面板进入
  failed 时保留 30 秒自愈观察窗口；真实步骤失败后重试成功才可使用
  `PASS_WITH_RETRY`。
- 只使用本次运行生成的 `run_N.json`、`evidence.json`、`trace.json`
  和 UI 录屏取证。不得用旧报告补齐缺失事实。

## 判决约束

- Runner 不再生成业务机械判决。`mechanical_verdict` 为兼容字段，固定为
  `PENDING_AI_REVIEW`；`checks` 为空。退出码只说明执行，不能据此判定功能通过。
- 对所有功能用例，脚本结束后继续读取本次 `evidence.json.expected` 和 `acceptance`，
  对照原文、最终产物、trace、SSE 和 UI 证据逐项验收；必要时独立回读平台。
  不需要等待用户再次提示。保留原始 JSON，在 report.md 填写规则、观察、证据、结论、验收人和时间。
- 分开看 `execution_status`、`evidence_status` 和 `evidence_issues`，区分产品缺陷、
  可恢复重试、基础设施错误和证据不足。结论为 PASS / PASS_WITH_RETRY / FAIL / INCONCLUSIVE；
  证据不足不得猜测通过。只有明确的失败重试恢复证据才可使用 PASS_WITH_RETRY。
- 非当前专项的异常写入失败描述或备注，但不得改变该专项测试结果。具体专项范围
  以架构文档“判决模型”为准。
- 性能统计只聚合成功用例；末次仍失败的用例保留在失败清单，不写入正常链路数据。

## 所有功能用例的通用 AI 验收

- 使用本次 evidence.json 中原样保存的 expected、acceptance 和 scenario_snapshot，
  rule_source 提供规则路径与哈希。不将运行后修改的 YAML 当成本次标准。
- 遍历全部规则，包括脚本从未识别的新业务字段。expected.publication 中的平台与顺序
  是执行配置；其他内容按实际含义判断，不要求字段对应 Python 校验函数。
- 每项记录预期、观察结果、证据引用和结论；无法理解规则或缺少必要证据时记录
  INCONCLUSIVE 和具体原因，不跳过后汇总为通过。
- 先核对执行与采集状态，再使用源文档、最终产物、session、trace、SSE 和 UI 证据。
  脚本没有采集的页面显示或远端结果，可按需使用 CLI、技能或浏览器补充只读取证。
  不得为了验收重新发布或改写目标。
- 以下平台定位、独立回读和报告留证要求也适用于 R 系列及其他涉及平台写入的用例。
  非平台用例不强制回读；非 UI 运行不能凭日志推断实际页面显示正常。

## 发布后 AI 验收（W01）

W01 的脚本仅执行串行 UI 发布并保存回执，内容验收由运行测试的 Agent 在脚本结束后
继续完成，无需让用户另行提示。先读取本次 `evidence.json.acceptance`；若本次没有
保存规则，查阅 YAML 并明确规则来源。不得把 `mechanical_verdict=PASS` 当作业务通过。

1. 读取本次 `final.md`、`publications.json`、`run_N.json` 和报告的目标表。确认平台
   执行状态，原稿只作为比较基准，不恢复原稿或重新触发发布。
2. 根据本次回执选择 CLI、相应技能、已有独立回读工具或浏览器读取目标；按所用技能
   要求执行。不得用产品返回的正文、旧基线或其他批次文档冒充独立回读。
3. 逐项检查本次 acceptance：正文和章节、表格表头及行列对应、有序/无序列表、JSON
   代码、配图和图片显示。允许正常格式转换和图片重托管，检查连续发布是否累积丢失。
   纯文本不足以证明结构和图片正确，必要时读取原生块或打开页面。
4. 平台定位：GitHub 使用实际写入提交/PR 分支，不用默认分支替代；微信使用草稿 ID
   和文章索引，不把后台首页/阅读原文链接当预览；Obsidian 核实实际 Vault、文件和
   附件路径；飞书/Notion 使用本次创建文档的链接或 ID。
5. 在本次输出目录保存必要的回读摘录或截图，记录读取方式、时间、目标版本及证据路径。
   不输出凭据或签名 URL；不要求固定解析器或额外创建新的自动回读脚本。
6. 更新 `report.md` 的逐平台业务验收列及说明：逐项结果、证据引用、结论
   PASS / FAIL / INCONCLUSIVE。保留原始执行状态、所有目标链接和标识；技术结果与
   业务判断分开。未配置/未执行、权限不足、回读失败或无法确认的项写明证据不足，
   不推断通过；一个平台阻断不妨碍核查其他已发布的平台。

报告必须列出每个平台的文档或地址链接（如有）、目标 ID、相关路径/提交/草稿索引，
方便人工复核。AI 判断后在报告记录验收人（Agent）和时间，不覆盖原始 JSON 证据。
若按测试表上报，证据不足的平台使用表内已有的“待测试/阻断”等状态，不填 PASS。

## 性能统计与上报约束

执行性能测试、重算或上报前，阅读 README 的[当前性能统计口径](README.md#当前性能统计口径)
和[性能统计核验](README.md#性能统计核验)。

- 根据实际部署的模型调用与 tracing 实现确认 usage 是单次值还是实例累计值，显式
  设置 `E2E_LLM_USAGE_SEMANTICS` 并检查产物元数据。不能仅凭 token 递增或沿用
  上一批设置决定口径；历史 trace 缺少声明时，必须先找到实现依据再重算。
- 累计 usage 先按完整调用序列差分，再划分失败尝试；不得先删失败 span 再差分。
  保留原始 observation，用 `usage_corrections` 记录修正；基线不明时不得猜数上报。
- 阶段只统计 advance_step 完整执行子树，包含内部 Agent 决策；步骤外发起决策只计
  全流程。不使用旧时间窗、旧函数名或外部决策并入阶段的逻辑兜底。
  串行轮次关联仅用于失败发起决策分类及步骤内 workspace 重试边界；失败关联有歧义
  或冲突时记录 unresolved_dispatches 并阻止正式导出。原始 trace 不改写。
- 排除失败墙钟时包含晚于父 span 结束的失败后代，并保护与保留工作重叠的时间；
  不逐项减去并行时长。runner 批次耗时与 trace 耗时分别记录，主报表总值不剔除失败重试。
- 全流程保留统计时间边界内阶段之间的正常调用，不能因其未归属阶段而剔除。
  主报表阶段及全流程总值保留成功样本的全部尝试，显示为 `总值(失败重试部分)`；
  没有异常贡献时只显示总值，不另增异常行列。底层 `reported` / `reported_full` 及
  `failed_retry` 均为数值；`normal` / `clean_full` 只用于审计。全文字符数只取最终稿。P04/P05 修改文字数按同次 trace 完整原文与最终稿的可见字符新增＋删除计算；缺失或歧义不得猜数，详见 README「修改文字数采集」。导出为 15 列，修改量在键前；上报须匹配新列顺序。
  总值与括号必须使用相同样本和时间边界，终态后正常尾迹不能算作失败重试。
  阶段和全流程按各自口径核验，不要求全流程等于阶段之和。
- 完整 LLM 诊断字段仅用于补充归因，按 README 的诊断采集说明核验完整性。
  不预计算分项字符；离线分析正文即可。原有字符列仍累计已采集 IO 载荷，
  不叠加诊断正文；响应计时及 HTTP 重试记录不替代阶段边界或业务重试过滤。
- 上报前做原始 trace 到统计结果的独立核验；表格回读一致只验证写入，不能证明
  token 语义正确。历史批次口径不一致时，同口径重算后再归因，不擅自覆盖历史批次。
- 重算时同步更新本批次 `stats.json`、`stats.md`、`sheet_rows.json`、`summary.md`
  和相关分析。上报本批次时同步检查「测试批次」「全数据」「对比」，保留用户选择
  的基准批次；用不同场景核验引用和变化率。默认无须保留旧版报告备份或重复上传副本；用户明确要求备份时按其要求执行，
  保留最终报告、原始运行证据及必要的简短上报说明。

## 安全与维护

- 不在报告、终端摘要或提交中泄露 token、Authorization、签名 URL、密码或 provider 凭据。
- 不修改 `tests/e2e_old/`，不为通过测试而改写用例结果或业务数据。
- 更新测试脚本后，按改动核验语法、输入输出契约及原始证据到指标的计算；只报告实际完成的检查。
- 上报飞书表格前先回读真实表、字段、选项和视图；按 README 的上报入口执行，写入后再次回读校验。
