# F5 单步验收材料：对应验收文档 2.5

依据：[验收文档 2.5](https://sensetime.feishu.cn/wiki/INOBwfjnqiNGxNkTXGAcxJT6nUh)，读取版本 revision_id=342，日期 2026-10-23。只覆盖新稿、大纲确认、正确引用、局部修改和导出完整性；不包含第 3 节额外路径或 F6 审稿。

## 文件与入口限制

- `f5-research-materials.md`：用户要求的完整可上传 Markdown，包含研究说明、综述、三篇真实文献目录、方法和模拟结果。上传时明确这是研究材料，不是稿件或大纲。
- `f5-paper-input.json`：同内容的业务对象，含 question、evidence、papers、writing_materials、constraints、analysis 和 charts；用于完整引用链路的测试。不是 step/input/output 包装，不需要运行 F1–F4。
- `chart-rates.svg`、`chart-deltas.svg`：五项成功率对比与百分点差值图；JSON 中已内嵌完全相同的 SVG。
- `f4-source-data.json`：用于复验来源哈希的规范化 F4 输入，含 10 行模拟汇总数据；不是 F5 入口文件，无需上传。
- 本 README 是测试操作说明，不要作为研究附件上传。

**当前实现**：Markdown 原文通过通用文件读取进入 writing_materials，不做用途或章节分类。自产 F2 格式文献目录按需生成临时引用目录，与 papers 的书目共同支持 Writer 引用；不新增 references 池字段。

JSON 附件可直接读取业务对象或前序步骤导出的 pool。多份 JSON 时应明确文件名，由 paper_input 选择；不会根据旧执行历史自动选择步骤。通用文档也支持 PDF、Word 等格式，是否可完整提取正文取决于公共读取器。

Markdown 入口请同批上传 Markdown 和两张 SVG；相对图片链接不会替代图片附件。JSON 入口只绑定 f5-paper-input.json，不重复上传 SVG，也不要绑定 f4-source-data.json。

两种入口分别在新会话测试，避免已确认材料池优先级导致读取旧材料。JSON 已包含 Markdown 全文，不需同时上传二者。

## 操作一：研究材料 → 独立大纲

同批上传 Markdown 和两张 SVG 后，@论文写作工作流，发送：

> 附件 f5-research-materials.md 是研究材料，不是已有稿件，也不是大纲。根据这些材料写一篇介绍我们 LazyMind 论文写作 agent 的中文论文。先给我看大纲，确认后再写正文，引用用我提供的文献。只测试 F5，不运行实验，不做前置选题、文献检索或数据分析，不进行审稿。实验数字是模拟测试材料，请明确标注；文献目录若未被入口识别，请明确说明，不补造引用。

如果使用已经正确绑定的 JSON 入口，第一句替换为“请使用 paper_input 中的业务材料”，并保留其余要求。

检查只进入 write_outline，展示独立可编辑大纲，生成后停下。研究材料中的章节不是确认大纲，不应将整份材料照抄成大纲；也不应将材料判为 manuscript 而进入 revise_document。

## 操作二：确认大纲 → 正文

在大纲中确认包含引言、相关工作、方法、实验与模拟结果、局限性及结论。可将“实验”改名为“实验设置与模拟结果”，保存以检查所选版本是否生效。随后发送：

> 我已确认保存的大纲。请严格按这个版本完成中文论文正文，保留两张结果表、两张已有数据图和模拟数据声明，引用只使用提供的三篇文献。完成后停止，不审稿，不修改已确认大纲。

检查标题结构及顺序沿用确认版本；稿件完整；模拟数据没有被包装成真实运行结果。两种入口均应出现三篇文献的正文引用及完整书目；引用身份必须分别映射 ref-react、ref-self-refine、ref-lost-middle，编号按首次出现顺序，不预先要求固定编号。

对正文人工核查：

| 核查对象 | 必须符合的材料事实 |
| --- | --- |
| 研究对象 | LazyMind 的材料追溯、大纲确认、局部修订，不是新模型训练 |
| 文献解释 | ReAct 支持交互背景，Self-Refine 支持迭代修订背景，Lost in the Middle 支持上下文利用问题；不替本文提供性能证据 |
| 模拟结果 | 引用正确率 75%→95%，差 20 个百分点；非目标章节 84%→98%，差 14 个百分点 |
| 失败保留 | B1 仍有 3 个引用位置错误、2 个非目标章节变化；不能写成零错误 |
| 证据边界 | execution_status=not_run；无显著性、置信区间、真实模型性能声明 |
| 数据保留 | 两张结果表均完整；分母和未达标数量一致；两图各插入一次、正文各有引用，图中数值与表格一致 |

保存正文并导出原稿作为后续对照，记录本次实际模型配置、时间和软件版本，不把本目录模拟数字当作本轮测量。

## 操作三：只修改引言

发送验收文档原要求：

> 引言太像产品介绍了，改成研究论文的写法，其他章节先别动。

应进入 revise_document，使用现有稿件和本轮要求，不自动先审稿。引言应围绕研究背景、问题和贡献组织，避免“强大平台”“全面赋能”等产品宣传语。即使初稿已较学术，也应判断实际修改是否满足请求，不以主观判断代替前后对照。

保存并导出修订后的全文。逐章节比较修订前后 Markdown：引言应有符合请求的实质改动；其他正文、两张表格、两张图及实验数值应保持不变。换行统一为 LF 后比较。受管引用编号若因引言引用顺序改变而重新排序，核对来源 ID 和书目内容；不要把纯编号变化误判为论述变化，也不要忽略真实的文字改动。大纲仍为确认版本。

## 操作四：导出完整性

打开下载文件检查：标题、全部章节、最后一段结论、两张表格、两张可见图片及三条书目均存在；导出内容与保存的最终稿对应，不能只有引言、预览片段或上一版本。若使用 DOCX/PDF 等界面实际支持的格式，也检查表格跨页与文献可读性；未提供的格式不列为已通过。重新打开会话核对保存内容。

逐项记录通过/失败/未测试及证据；任何模型报错、引用目录缺失或导出缺失均保留实际记录，不用模拟表格代替结果。

## 代码与格式依据

路径相对于仓库根目录：

| 实现 | 本材料采用的约定 |
| --- | --- |
| algorithm/lazymind/document_tools/paper/contracts.py | question/evidence 为文本，papers 来源列表；F5 不直接消费 experiment 字段 |
| algorithm/lazymind/document_tools/paper/literature_review.py | 当前实际输出 evidence Markdown 和 papers；不沿用旧样例 evidence 对象 |
| algorithm/lazymind/document_tools/paper/writing/attachments.py | Markdown 分类；materials 汇入 writing_materials；不是 Markdown 内嵌 JSON 解析器 |
| algorithm/lazymind/document_tools/paper/writing/common.py | citation 非空才进入 reference_candidates；方法与实验说明放入 writing_materials |
| algorithm/lazymind/document_tools/paper/writing/write_outline.py | 独立大纲入口 |
| algorithm/lazymind/document_tools/paper/writing/write_document.py | 已有大纲是正文前置条件，正文只输出 manuscript |
| algorithm/lazymind/document_tools/paper/writing/revise_document.py | 本轮修改意图、范围定位和受限替换 |
| algorithm/lazymind/document_tools/paper/workflow.py | pool_from_business、business_input、initialize_pool 与实际业务对象绑定 |
| workflows/paper-writing/workflow.yaml | 新稿先大纲、确认后写作，直接修改走 revise_document |

本材料不设置 outline/manuscript/review；前两者等待本次 F5 生成，不进入审稿。新增 analysis 包含两个分析目标、15条 findings（10项成功率、5项百分点差），charts 含两个图对象。它们是符合 F4 契约的人工构造输出，并非一次真实 F4 模型执行结果。analysis 的来源哈希由 f4-source-data.json 规范化输入计算；findings 不是顶层字段。JSON 路径使用 chart-rates/chart-deltas 图 ID；Markdown＋SVG 路径由附件适配器按内容哈希登记 image-* ID，不能要求两条入口的图 ID 相同。word_target=null，以免额外引入严格篇幅测试。

已完成的离线检查：JSON 可被 pool_from_business 和 write_outline 输入契约接受；3 个唯一来源均有 citation；Markdown 与 JSON.writing_materials 相同；表中 10 个百分比及差值/未达标计数一致；analysis 来源哈希、15条发现及两个图的列/发现引用通过当前 F4 校验器检查，内嵌SVG与独立文件一致。未运行真实模型、前端和导出验收。

## 文献格式更新

测试 Markdown 已采用当前 F2 提示词约定：正文 `[source_id]`，末尾 `## 参考文献` 下逐项 `[source_id] citation`，不再以 title/authors/citation 键值清单表达文献。正文仍是 F1–F4 综合测试材料，不声称整份文件由 F2 实际运行生成。此格式调整尚不代表当前 F5 已实现书目解析；实现方案见 ../../F5-IMPORT-PLAN.md。
