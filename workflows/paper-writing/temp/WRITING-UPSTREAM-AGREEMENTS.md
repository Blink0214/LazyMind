# F5 材料与字段说明

更新：2026-10-23；对应当前代码 `47393a9ad`。以 `paper/contracts.py`、`paper/review_schema.py`、`paper/analysis/schema.py` 为字段校验依据（均位于 `algorithm/lazymind/document_tools/`）。本文只说明 F5 消费及产出，不重新定义上游完整模型。

## 入口与返回

| 入口 | mode | 算法返回（固定字段） |
| --- | --- | --- |
| write_outline | draft | outline、is_demo |
| write_document | draft | manuscript、is_demo |
| revise_document | revise | manuscript、is_demo |
| peer_review | 不使用 F5 mode | review、review_data、is_demo |

mode 由入口确定，不再以“draft＋已有成稿”隐式转到修订。write_document 必须有大纲且不能已有成稿。正文写作与修订都不返回 outline，不新增其材料版本。

## F5 公共输入

下表描述归一化后的业务参数；算法不读取 pool、slot 或文件路径。并非要求终端用户手工上传这些字段。

| 字段 | 类型/用途 |
| --- | --- |
| topic | 非空主题文本 |
| request | 本轮要求文本；允许输入层缺省，修订另要求明确本轮意图 |
| mode | draft 或 revise，与入口一致 |
| constraints | paper_language、word_target、paper_type、citation_style、focus |
| papers | 来源对象列表；缺省 [] |
| question、evidence | F1 研究主题、F2 综述的 Markdown 文本或 null；F5 不接受旧候选/证据对象冒充文本 |
| analysis | F4 结构化分析或 null；遵循现行 Analysis 校验 |
| charts | F4 Chart 列表；缺省 []，不使用旧单图 chart 字段 |
| writing_materials | 上传 Markdown 研究材料汇总文本或 null |
| media_assets | 公共 Writer MediaAssetLibrary 或 null，承载用户图片 |
| outline | Markdown 或 null；正文生成要求非空有效大纲 |
| manuscript | Markdown 或 null；修订要求已有稿件 |
| review、review_data | 审稿报告 Markdown 和 SavedReview；按审稿修订时必须成对有效 |
| selected_issue_ids | 内部 ID 列表，缺省 []；直接修改为空，工作流按审稿修订时由代码填入当前全部 ID，不是用户选择槽位 |

constraints 默认：paper_language=zh-CN，word_target=null，paper_type=null，citation_style=GB/T 7714，focus=[]。语言支持 zh-CN/en；word_target 为正整数或 null。新稿按语言统计篇幅并检查目标 ±10%；局部修订不强制检查全文字数。citation_style 不代表实现了所有引用风格的完整渲染。

## 文献与引用

- papers 保留上游 ID；缺失/空 ID 按来源内容生成稳定 `ref-<摘要>`，重复或冲突不静默合并。
- title 及 text/abstract 的有效性由公共契约检查；仅有摘要不代表阅读全文。metadata、reading、kind 等扩展信息保留。
- citation 为可选非空书目条目。没有 citation 的材料可供阅读，但不进入新增引用候选目录；不凭空补写作者、年份、DOI。
- 新增正文引用使用 `[文献:Paper.id]`。明确匹配来源目录的旧 ID 标记可由代码规范化；无映射的数字或作者年份引用不猜配。
- 发布按首次引用顺序生成数字链接 `#paper-ref-Paper.id` 和文末受管参考文献区域；引用身份与显示编号分开。
- 模型不得改写受管参考文献区；修改后由发布程序重编号和重建列表。未知来源、无条目引用、损坏区域或不支持的混编会被拒绝。

## 分析、图表与用户图片

- analysis 使用现行 F4 输出，F5 不重新计算数据或增加分析模型。findings 的 id 与 charts.finding_ids 关联，未知关联被拒绝。
- F4 Chart 消费字段包括 id、title、svg、caption、columns、finding_ids；完整规则及来源校验归属 analysis/schema.py 等上游实现。
- SVG 通过校验后按内容摘要发布，正文使用已登记图片地址；发布不能证明实验真实性或统计结论正确。
- 普通上传图片保存在 media_assets，保留公共媒体结构，并记录内容摘要及源文件名。内部 figure_catalog 将其映射为统一图片目录，不伪造 F4 Finding 或 columns。
- 大纲使用 `<!-- paper-charts: ["图片ID"] -->` 记录章节归属；上传大纲中的明确图片链接可转换为归属标记。缺失归属可补齐，已有标题和归属不得被移动。
- 正文使用带 `block-ID` 锚点的图片对象及正文引用。内部占位符发布为稳定地址；不允许未知图片、重复图对象或悬空引用。
- 图片归属是内部规划信息，不作为正文段落展示，也不因正文生成而覆盖确认大纲。

## 审稿结构与版本

SavedReview 固定包含 summary、issues、limitations、manuscript_sha256、review_sha256。每条意见包含：

| 字段 | 含义 |
| --- | --- |
| id | R1、R2…，代码按严重度排序后生成；展示报告与结构化结果一致 |
| severity | major / minor / suggestion |
| category | method / evidence / analysis / structure / language / citation |
| location | 章节位置文本；修订时结合 quote 解析为公共 Writer Markdown 定位 |
| quote | 正文原文摘录或 null；非空时审稿端检查可在稿件定位 |
| problem、suggestion | 问题依据与最小可执行建议 |
| action_type | text_revision / needs_material / needs_experiment |

没有 decision 或预存 target 字段。修订侧解析出的 target 是内部临时定位数据，不要求 F6 生成。

采用审稿时，两个 SHA256 分别与当前稿件和报告精确匹配；编辑任一文本后不能继续使用旧配对意见。版本匹配只证明适用性，不能证明用户要采用审稿。

意图模型只返回 direct/review/unclear。direct 排除历史审稿；review 取当前完整意见列表；无意见或要求部分意见时提示，不自动当成直接修改。内部保留 ID 与修改计划关联、范围及可执行文字意见覆盖检查。

text_revision 要实际修改文字；needs_material/needs_experiment 仅作现有材料支持的文字调整。没有可执行修改时停止，不生成虚假完成结果。没有独立的“逐条意见已解决”输出协议。

## 附件与运行上下文

- JSON 业务附件通过既有 paper_input 适配；带 step/input/output 的说明样例不是直接上传对象。
- Markdown、SVG/PNG/JPG 由写作侧附件适配读取，Markdown 分类为 outline/paper/materials/manuscript；图片复用公共媒体收集。SVG 摘要来自文本标签，不是视觉推断。
- 不新增 paper_files 或 revision_options slot。确认后的 pool 或显式业务绑定保持优先，不静默用新附件替换。
- 本轮要求通过现有 user_input/current_user_input 传入，重试按现有规则恢复要求；当前查看的 tab 不代表执行节点或修订意图。
- pool 内部保存材料、版本和来源关系；`<step>_pool`、`<step>_document` 是运行发布结果。算法输出字段与这些 slot 分开。

当前上传样例见 [examples/README.md](examples/README.md)，实际验收状态见 [F5-IMPLEMENT.md](F5-IMPLEMENT.md)。样例合法或字段校验通过均不代表真实端到端验收通过。
