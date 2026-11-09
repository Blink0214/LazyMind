# F5 当前实现与验证状态

更新：2026-10-23；代码基线 `47393a9ad`。本文描述已有实现，不将历史设计或自动化替身测试当作真实链路验收。

## 节点与输出

| 节点 | 前置条件与行为 | 算法成功输出 |
| --- | --- | --- |
| write_outline | 新稿材料或已有大纲；生成/补齐大纲和图片归属，等待用户编辑确认；拒绝已有成稿 | outline、is_demo |
| write_document | 已有大纲且没有成稿；章节规划、正文生成、结构/引用/字数及一致性检查 | manuscript、is_demo |
| revise_document | 已有成稿及本轮修改要求；直接修改或采用当前完整审稿 | manuscript、is_demo |

每个节点另由工作流发布 `<step>_pool` 和 `<step>_document`。这些是运行产物，不是算法返回字段。

## 大纲确认与正文写作

工作流使用独立“论文大纲”节点和展示页，成功后等待确认。用户编辑保存的大纲通过 `write_outline_document` 与材料池一起回流，形成当前 outline 材料；正文按选定版本继续。

正文入口要求已有大纲。它仍复用 `prepare_outline()` 补齐缺失图片归属，但不得改动已有标题层级、顺序和图片分配；归属完整时不重新生成大纲。补齐归属用于本次写作规划，不作为新大纲输出。

正文只输出 manuscript，材料池保留确认大纲的内容和版本，不再从正文标题重建大纲。正文标题仍须与输入大纲一致。工作流适配器检查确认大纲的绑定；独立算法 API 本身不持有前端“用户确认”状态。

## 修订行为

- 无当前审稿时，现有入口直接使用本轮要求，不额外调用审稿意图模型。
- 有审稿时，`resolve_review_issues()` 的模型仅返回 direct/review/unclear，不返回意见 ID。direct 不带入历史审稿；review 由代码读取当前报告的全部 ID；不明确或要求部分意见时提示澄清。
- 内部字段 `selected_issue_ids` 和 `selected_issues` 仍用于现有契约及计划范围约束，不代表实现了前端勾选，也不要求用户填写。
- 使用审稿时，报告和稿件的 SHA256 必须匹配；匹配不能代替用户采用审稿的意图。
- 审稿意见直接用于公共 Writer 修改计划，不重新审稿。text_revision 要有实际文字修改；needs_material/needs_experiment 只允许已有材料能支撑的文字调整，不能编造补充数据或已完成实验。
- 没有审稿意见，或公共 Writer 未能为补材料/实验意见形成可执行修改时，明确提示并停止。当前没有逐条处置结果的独立输出。
- 保留意见定位、操作范围、非重叠替换、有效修改及图表/引用检查。修订不执行全文一致性模型检查，也不强制全文字数目标；大纲不更新。

## 文件职责与公共 Writer 复用

以下代码路径以仓库根目录为起点。

| 文件/模块 | 职责 |
| --- | --- |
| algorithm/lazymind/document_tools/paper/writing/write_outline.py | 大纲生成、已有结构和图片归属保护 |
| writing/write_document.py（同一 paper 目录） | 章节规划、正文流式生成、组装和验证 |
| writing/revise_document.py | 审稿使用意图、修改计划与替换范围保护 |
| writing/common.py | 材料与 Writer context 适配、章节预算、图表及引用约束、阶段追踪 |
| writing/attachments.py | Markdown 用途分类、图片导入、初始化业务材料 |
| paper/analysis/attachments.py | JSON 附件适配，保留业务材料供下游使用 |
| paper/contracts.py、api.py | 输入归一化、版本与结果契约、调用算法 |
| paper/workflow.py | 材料池、编辑回流、本轮要求、图表与文献发布、产物保存 |
| paper/publication.py、runtime_events.py | 资源/引用发布、进度与预览事件桥接 |
| frontend/.../WorkflowPanel/writerArtifactStream.ts | 大纲/正文/修订流的关联、延迟兜底、失败预览隐藏 |

上述 `paper/` 简写均相对于 `algorithm/lazymind/document_tools/`。

复用公共 Writer 的任务/context 构建、大纲与章节规划、正文生成和组装、一致性检查，以及修订定位、修改计划和字符串替换。普通图片复用媒体收集能力；SVG 先校验并提取文本标签，标签摘要不等于视觉理解。

论文侧负责材料池映射、图表归属和不可变图片资源、稳定来源引用、审稿版本及修改范围约束。没有复制独立写作模型，也没有新增 revision_options/paper_files slot。

## 流式展示与保存

大纲使用公共 Writer 的流式大纲接口；正文按公共章节流式接口显示。公共 Writer 的重试可通过既有 restart 回调更新预览；不能据本次 slot 修复宣称所有回退重播问题已解决。

修订沿用公共定位替换模式，完成校验后一次展示完整修订正文，不逐 token 展示替换内容。三个论文 slot 均参与流式任务关联及失败撤回。图表和文献发布后，pool 和 document 使用可保存的稿件内容。

## 验证记录及限制

- 最新相关 Python 回归 144 项通过；大纲流式适配前端 15 项通过。此前较大前端集合 104 项通过，属于不同轮次，不相加。
- 扩大检查曾有 8 项算法契约失败，对照提交前基线同样存在；不是全量回归通过。
- 真实 Markdown＋SVG 第一、三组完成过正文、图片和保存重开检查；第二组未完整通过。第一组曾出现正文偏短，不能写成全面验收成功。
- 真实直接修订完成过目标修改、无关内容保留及重新打开检查。
- 最近一次真实审稿约 177 秒后在 JSON 解析阶段失败，尚未进入修订；原始响应未取得，根因未确认。未用 mock 审稿代替验收。
- 最新“大纲保留、全文字数放宽、完整审稿修订”改动尚未重新完成真实前端验证。剩余验收见 [F5-PLAN.md](F5-PLAN.md)。

上传材料统一在 [examples/README.md](examples/README.md)。历史日志、截图、备份和重复 fixture 已清理，以上保留结果摘要，不再引用已删除文件。
