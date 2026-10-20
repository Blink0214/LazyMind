# 旧论文流程可复用能力（只读调查）

日期：2026-10-15。检查对象：LazyMind/workflows/academic_research_pipeline 及已使用的 document_tools 公共能力。
本文件为临时开发交接资料，整个 temp/ 目录须在提 PR 前删除。没有修改旧 workflow，也没有把旧算法接到新流程；“可复用”是代码检查结论，不等于真实模型效果验收。

## 结论

2026-10-20 F6 接入更新：新 `document_tools/paper/peer_review.py` 使用公共 `AutoModel(model='llm')`，复用 WriterToolBase 的结构化提示和 JSON 解析。已不再调用其默认全文重试入口：F6 自行汇总格式/摘录错误，只修复出错的字段或意见一次，有效意见保留；修复摘录由代码按原文行号复制，合并后严格校验，内部最多两次模型调用。提示词在 `document_tools/paper/prompts/peer_review_prompt.py`，从旧 peer_review 的检查维度、原文依据和最小修正思路适配，保留来源与非商业许可说明；没有复制五席位、旧槽/工作区、固定修订轮次，也没有反向导入旧脚本。旧 `audit_academic_manuscript` 尚未接入。

真实模型调用、此前浏览器链路及最新局部修复回放已有验证，但不代表审稿质量或最新端到端验收通过；新工作流已删除 prepare 节点，上传论文可直接进入 peer_review。按用户要求删除模型替身 conftest，F6 调用测试使用配置的真实模型；格式校验和排版直接调用对应函数测试。Chat 简短收尾目前只有提示词约束，仍有重复读取报告的问题。商业发布前仍需确认来源授权。

F5 的复用价值最大：旧流程的写作桥接本来就在调用公共 Writer 能力。应优先复用 document_tools 中的能力，在新 paper 模块中做业务适配，而不是依赖旧 workflow 的工作区、节点名、固定两轮审稿和文件槽。
F1/F2/F6 有大量可参考提示词与约束，但并不是现成的独立 run(inputs) 算法；F3/F4 没看到对应实验执行/统计绘图实现。

## 可复用清单

| 新功能 | 已有实现／出处 | 可复用内容 | 接入时要处理 |
| --- | --- | --- | --- |
| 入口／写作要求 | pipeline_tools.py:normalize_academic_parameters（76） | 论文类型、目标篇幅、中英文、引用风格、MD/DOCX 规范化 | 旧入口六项必填，不能原样强加给所有新功能；按模块收敛参数 |
| 研究选题 F1 | scenario/state.yml:formulate_research（29 起） | 主问题/子问题、FINER、范围、候选贡献、方法蓝图提示 | 提取成核心算法提示并处理业务输入；不是已封装函数，不能宣称直接调用即可 |
| 文献检索／综述 F2 | scenario/state.yml:retrieve_literature、synthesize_evidence（80/142 起） | academic_search/kb 调用策略、检索预算、证据去重、核验状态、主题综合、主张-证据映射 | 检索能力在现有工具中；旧流程的去重、综合主要靠提示词；与读侧约定论文对象与来源 ID，需新适配 |
| 写作上下文 F5 | writer_bridge.py:academic_writer_prepare_context（114） | 收集完整证据并构建 writing_task/resource_profiles/writing_context | 优先调用公共 WriterCreateToolkit 的 build_writing_task/build_resources/profile_resources/create_writing_context；剥离旧 require_context、工作区与旧槽依赖 |
| 大纲 F5 | writer_bridge.py:academic_writer_generate_outline（176）、academic_writer_normalize_outline（436）；pipeline_tools.py:validate_and_allocate_academic_outline（214） | 生成大纲、以用户编辑的 Markdown 为准同步结构、精确分配篇幅、校验证据 ID | 现有大纲结构是 chapters/children；新模块需明确结构与正文边界，不强制所有科研阶段走大纲 |
| 正文 F5 | writer_bridge.py:academic_writer_plan_sections（598）、academic_writer_write_sections（814）、academic_writer_assemble_draft（864） | 章节规划、流式成稿、章节检查点、标题对齐、汇总正文 | 底层调用公共 WriterCreateToolkit；旧桥接有文件路径和事件依赖，不整文件复制；算法/运行适配各归其位 |
| 修订 F5 | writer_bridge.py:academic_writer_revise_from_feedback（990）、academic_writer_revise_markdown（894）；document_tools/revision.py:revise_markdown_document（37） | 根据意见改完整 Markdown、保留无关内容、限制引用范围、失败保留原稿 | 公共 revise_markdown_document 已不依赖 Workflow context；需适配稿件/意见、模型配置及 artifact_store；新模块返回正文，不返回旧槽路径 |
| 模拟审稿 F6 | pipeline_tools.py:audit_academic_manuscript（388）；scenario/state.yml:peer_review（371 起） | 前者是篇幅、引用 ID、章节与声明检查；后者是五视角评审和问题路线图提示 | 规则检查可以抽取复用；语义审稿要封装算法，不能把规则通过当作科研结论正确，也无需照搬五席位/固定两轮 |
| 导出 F5 | document_builder.py:_markdown_to_docx（154）、compose_academic_paper_from_inputs（209） | Markdown 保存及基础 DOCX：标题、段落、列表、表格、页码 | 入口绑定旧槽；核心渲染可抽取。未见完整公式/图片处理，不能承诺完整论文排版或 PDF/LaTeX |
| 阅读心得 F7 | synthesize_evidence 提示词 | 多论文比较、共识分歧、研究启发可借鉴 | 未发现专门“阅读心得”函数，需要结合读侧正文实现 |
| 实验／分析 F3/F4 | 本次检查的旧流程中未发现对应执行器 | 只有方法论建议，不是实验执行或统计绘图 | 独立开发或接现有执行服务；F3 为 P1 |

上述行号对应本地调查时版本。路径基准：
- [writer_bridge.py](../../../workflows/academic_research_pipeline/scripts/writer_bridge.py)
- [pipeline_tools.py](../../../workflows/academic_research_pipeline/scripts/pipeline_tools.py)
- [document_builder.py](../../../workflows/academic_research_pipeline/scripts/document_builder.py)
- [state.yml](../../../workflows/academic_research_pipeline/scenario/state.yml)
- [公共 writing.py](../../../algorithm/lazymind/document_tools/writing.py)
- [公共 revision.py](../../../algorithm/lazymind/document_tools/revision.py)

## 本轮已经复用的基础设施

这部分属于新骨架迁移，不是旧论文算法接入：
- document_tools.artifacts.markdown_filename：中文文稿文件名处理。
- document_tools.artifacts.persist_artifact_json：使用现有 Writer JSON envelope 保存检查点；入口继续兼容旧裸 JSON 和新的 envelope。
- Runtime save_artifacts、既有写作控件、原生 rewind/recovery_inputs：保持已有机制，不另建发布或编辑链路。
- PPT 的声明式 UI 模式：保留 hide_when_material 不变，新增通用 ui.hide_skipped_steps 开关；论文启用，由 Runtime pruned/bypassed 驱动。没有生成额外 skip 文件，也没有在前端写死 paper-writing ID。

## 限制

只做代码和自动化检查，没有启动旧论文流程调用真实模型。核心论文模块本轮仍为演示实现。旧论文脚本没有修改，后续接入先从 F5 的共享能力开始；禁止直接从新 document_tools 反向 import workflows/academic_research_pipeline 下的脚本。
