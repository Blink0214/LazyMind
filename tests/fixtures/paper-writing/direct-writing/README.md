# 上传 JSON 直接验证 F5：图表、文献与后续审稿

以下两份为本轮推荐上传样例，均是人工构造的合成材料，不是真实论文或实验结果。JSON 顶层直接为业务对象，不带 step/input/output、mode、pool 或文件路径。两份均进入 **write_document / draft**；“已有稿件修改”不是 revise_document。

| 场景 | 上传文件 | 主要检查 |
| --- | --- | --- |
| 新稿写作 | [01-write-from-materials-with-chart.json](01-write-from-materials-with-chart.json) | 三张图、两个来源、首次引用排序及重复引用去重 |
| 已有稿件修改 | [02-edit-existing-with-charts-and-references.json](02-edit-existing-with-charts-and-references.json) | 保留已有图和稳定引用；新增图和更早引用，触发重编号 |

## 上传与入口

分别新开会话，上传对应 JSON，并输入：

- 新稿：“使用论文写作工作流，按上传 JSON 的材料和 request 直接写论文，跳过前序步骤。”
- 修改：“使用论文写作工作流，按上传 JSON 的 request 修改其中的 manuscript，跳过前序步骤。”

主 Agent 应把完整 JSON 内容绑定到 `paper_input`，选择 `write_document`。不要绑定为 `review_request`，不要使用 writing_input_file。无需另传 SVG、原稿 Markdown 或 supporting-data.json。文件 request 保留为首次写作要求，启动时的补充要求追加到其后。

本次只更新本地样例，不更新已发布工作流。手动测试前请确认运行环境已包含最新代码与适配的工作流包；旧会话可能固定在旧版本。

## 场景一：新稿、三张图、两份参考资料

outline/manuscript 均为 null。提供两篇合成文献、证据、选定问题、方法与分析结论，以及三张完整内嵌 SVG：

- chart-001：总体命中率，70.00% 与 81.67%。
- chart-002：加权平均延迟，120ms 与 128ms。
- chart-003：三个批次的命中率分布；不能据此声称统计显著。

request 要求先引用 ref-002，再引用 ref-001，随后重复引用 ref-002。因此，**若模型遵守该顺序**，发布后 ref-002 显示为 [1]，ref-001 为 [2]，文末仅两条。编号依据实际正文首次出现位置，不依据 papers 数组顺序。图号同样按最终图片位置计算；按要求顺序放图时为图1、图2、图3。

数值核对：同一组60个合成问题，基线命中42个，重排序49个；绝对差11.67个百分点，相对增长16.67%。延迟增加8ms，即6.67%。6条汇总行不是6个问题或独立重复实验。没有真实运行日志、尾延迟、置信区间或统计检验。

## 场景二：已有稿件、新增引用引发重编号

原稿对照：[02-original-manuscript-with-charts-and-references.md](02-original-manuscript-with-charts-and-references.md)，内容与 JSON.manuscript 完全相同，无需另行上传。

原稿包含：

- 一个已使用的 chart-001，使用稳定锚点和媒体占位符。
- ref-001 的数字链接及程序生成的参考文献区域；这些身份关系明确给定，不是从旧稿数字引用猜配而来。
- 三处故意保留的错误：百分点差写错、结果节遗漏延迟、讨论过度推广。

request 要求：在 ref-001 之前新增 ref-002；纠正三处错误；保留 chart-001，并在延迟段插入 chart-002；保留其他正文和既有参考文献区域，交由发布程序重建列表。

验收时检查：ref-002 变为 [1]、ref-001 从 [1] 变为 [2]；ref-002 重复使用仍只有一条文末记录。两张图片及其正文引用可用，没有重复插图。参考文献列表出现一次。未授权的摘要、方法、结论等内容不被改写。

原稿的图表使用占位符，是为了让 JSON 可在新环境独立上传，不依赖本机或某次运行的静态文件路径。执行成功后才生成稳定 SVG 地址；后续审稿应使用这次生成的成稿，不能拿这份初始占位稿当作已发布产物。对照 Markdown 直接打开时看不到图片是预期行为。

## 成稿槽位检查

1. 打开成稿页签，并查看/下载本次生成的「论文写作.md」。大纲仍在材料池中，没有独立展示槽位。
2. 图表实际可见；文件图片 alt 含图号，图片地址为 `/static-files/paper-charts/...svg`，无未解析媒体占位符。图号属性用于回流还原，不是新增 Chart 字段。
3. 正文引用显示 [1]、[2]，点击可跳转。源码链接保留 `#paper-ref-ref-ID`，其数字标签可以变化，ID 不变。
4. 文末只有一个受管参考文献区域，仅列实际引用来源；不残留 `[文献:...]`。图表 description 不拼入图题。
5. 不发布猜测的书目信息、不声称真实实验或统计显著。源数据附件仅供人工核对，不表示写作模型读取或复算了原始表。

## 后续审稿与再次修改

成稿成功后，在同一工作流继续选择审稿，要求：

> 请审查本次最新成稿：图表与数值是否一致，文献是否只支持对应的定义或评价原则，是否存在混淆百分点与相对增长、遗漏延迟代价、过度推广等问题。

使用运行层最新 pool/document 绑定，不重新上传初始 JSON，不复制旧稿覆盖当前成稿。审稿意见应对应这版稿件。原稿修改后，旧审稿意见不能用于新稿修订；需要重新审稿。

也可先通过 write_document 再做一次正文修改，然后重新审稿，检查图表、文献链接和列表是否保留。若要按审稿意见修订，需另走 revise_document 并选择该次审稿的有效 issue ID；不要将普通写作要求填入 review_request。

受管参考文献区域不能直接手改；文末条目来源是 Paper.citation。旧稿没有明确 ID 对应关系的引用不自动猜配或混编。

## 字段与验证范围

- Chart 固定 id/title/columns/svg/description；没有 caption 或 finding_ids。SVG 为内容字符串，不是路径、URL 或 Base64。
- Finding 使用 statement/columns/calculation/value/unit/sample_size，不添加 finding-ID。
- Paper 使用原有稳定 ID，缺失时由程序补齐；引用条目使用非空、无序号的 citation。首版正文固定数字制，citation_style 只控制上游条目格式。
- word_target=null，review=null，selected_issue_ids=[]。后续审稿结果由实际运行生成，不预置意见或哈希。
- tests/fixtures 与 workflows/paper-writing/temp/examples/direct-writing 中对应 JSON 保持一致。01-write-from-materials.json 与 02-edit-existing-manuscript.json 保留为无图基础样例。

本轮仅做离线 JSON、实际入口契约、SVG 安全、引用/图表发布与审稿输入组装检查。未调用真实模型，未进行桌面上传验收，未更新已发布工作流。历史 live-results 的结果不代表这些新样例已通过真实模型验收。
