# F5 上传材料：简化实现

本方案只修改 F5；不改变 F1—F4、F6 的输出格式和校验。

## 入口与材料

主 Agent 根据当前请求选择 write_outline、write_document 或 revise_document。导入器不通过旧 pool 的执行历史判断下一步。

- JSON：复用 paper_input，读取业务对象或导出 pool 的全部当前材料，在新会话重建材料记录，清空旧执行历史和意见选择。保留来源 ID、mock 标识、结构化审稿摘要；不重算摘要掩盖过期审稿。沿用现有业务契约，不再添加一轮逐字段校验。
- 文档：PDF、DOC/DOCX、PPTX、Markdown、TXT 等通过公共 resolve_text_target 读取正文，按文件名分隔后原样合入 writing_materials。不调用 LLM 分类，不拆分 question/evidence/analysis，不识别审稿章节，不重建 review_data。
- 稿件：主 Agent 明确绑定 manuscript_file/manuscript_text，沿用已有稿件入口；同批其他文档仍作为 writing_materials。
- 图片：沿用 media_assets 的落盘和 ID 映射，供 Writer 插图及最终发布使用。图片链接不能代替实际附件。

同一会话继续执行时优先使用已确认 pool，避免上传初始材料覆盖最新稿件或确认大纲。新稿仍先生成、确认大纲，再写正文；上传原文不会自动变成已确认 outline。

## 引用

按需从 papers 和材料中的自产文献目录生成临时引用目录，不新增 references 材料类型或 FIELDS 字段。

支持 F2 的 `[source_id] citation` 独立书目条目，以及 F5 已发布的受管参考文献区域。保留 ID、原书目与正文，不通过 LLM 补全作者、年份或 DOI，不猜配任意数字引用。保留书目冲突检查以及现有正文引用校验、编号和发布。普通文本中的未知方括号不会在导入阶段阻断整个文档。

## 写作与修订

两条输入路最终进入同一个 Writer 材料准备函数：

1. pool 中已有的 papers/question/evidence/analysis/charts/outline/manuscript 等按原契约读取。
2. 上传文档的 writing_materials 原文直接作为资源传入，media_assets 合入图片目录。

当前请求决定如何使用原文。提示词要求将审稿意见视为建议，不当作研究事实；仅在用户要求时应用，不编造缺失实验。F6 Markdown 无额外分类调用、专用分隔协议或版本校验；JSON review_data 仍走既有结构化修订和摘要校验。

F3 材料可以随 JSON 留在新 pool 中，本次不增加其到 Writer 的传递。

## 验证边界

离线测试覆盖通用文档读取器调用、原文进入三个 F5 入口的 Writer 上下文、JSON/pool 导入、自产文献编号发布、示例 Markdown + SVG 和结构化 JSON。实际 PDF/Word 的提取质量由公共读取器决定；模型生成质量及前端单步验收需实际运行确认。
