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

- `mechanical_verdict` 是 Runner/CI 的技术判决，不是测试表最终判决。机械
  `FAIL` 或 `INCONCLUSIVE` 必须调查，但测试表每一行只按对应专项测试项判定。
- 对每个 FAIL/WARN 建立证据链，区分产品缺陷、可恢复工作流重试、测试基础设施
  错误和证据不足。证据不足时写明 `INCONCLUSIVE`/“证据不足”，不得猜测。
- 功能报告的最终结论使用 `PASS`、`PASS_WITH_RETRY` 或 `FAIL`；基础设施阻断在
  测试表使用实际存在的 `ERROR`、`BLOCKED` 或 `待测试` 选项。
- 非当前专项的异常写入失败描述或备注，但不得改变该专项测试结果。具体专项范围
  以架构文档“判决模型”为准。
- 性能统计只聚合成功用例；末次仍失败的用例保留在失败清单，不写入正常链路数据。

## 性能统计与上报约束

执行性能测试、重算或上报前，阅读 README 的[当前性能统计口径](README.md#当前性能统计口径)
和[性能统计核验](README.md#性能统计核验)。

- 根据实际部署的模型调用与 tracing 实现确认 usage 是单次值还是实例累计值，显式
  设置 `E2E_LLM_USAGE_SEMANTICS` 并检查产物元数据。不能仅凭 token 递增或沿用
  上一批设置决定口径；历史 trace 缺少声明时，必须先找到实现依据再重算。
- 累计 usage 先按完整调用序列差分，再排除失败尝试；不得先删失败 span 再差分。
  保留原始 observation，用 `usage_corrections` 记录修正；基线不明时不得猜数上报。
- 阶段只统计 advance_step 完整执行子树，包含内部 Agent 决策；步骤外发起决策只计
  全流程。不使用旧时间窗、旧函数名或外部决策并入阶段的逻辑兜底。
  串行轮次关联仅用于失败发起决策剔除及步骤内 workspace 重试边界；失败关联有歧义
  或冲突时记录 unresolved_dispatches 并阻止正式导出。原始 trace 不改写。
- 排除失败墙钟时包含晚于父 span 结束的失败后代，并保护与保留工作重叠的时间；
  不逐项减去并行时长。runner 批次耗时与剔除失败后的 trace 耗时分别记录。
- 全流程保留统计时间边界内阶段之间的正常调用，不能因其未归属阶段而剔除。
  阶段只取最后成功尝试；阶段和全流程按各自口径核验，不要求全流程等于阶段之和。
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

- 不在报告、终端摘要或提交中泄露 token、Authorization、签名 URL、密码或
  provider 凭据。
- 不修改 `tests/e2e_old/`，不为通过测试而改写用例结果或业务数据。
- 更新测试脚本后，执行 README 中的 Python、Node 和 TypeScript 自测；只报告
  实际运行过的检查。
- 上报飞书表格前先回读真实表、字段、选项和视图；按 README 的上报入口执行，
  写入后再次回读校验。
