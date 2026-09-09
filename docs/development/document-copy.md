# 文档转换与复制

所有兼容 Markdown / WriterDocument 的写作产物使用共享转换能力。语雀等平台可复制 Markdown 源码，Overleaf 可复制 LaTeX 正文片段。复制不需要平台授权，不创建远程文档，也不改变写回状态。

## LazyLLM dependency layer

`WriterProviderBase.convert_document(content, *, target=None, media_assets=None, output_format='native')`

- `native` 保持具体 Provider 原有格式和行为。
- `markdown`、`latex`、`text` 使用 Base 的公共转换，返回 `WriterProviderDocument`，其 `provider` 为空，`content` 为字符串。
- 无平台实例时直接调用 `WriterProviderBase.convert_common_document(content, output_format='latex')`。
- 平台 native 实现在 `_convert_native_document` 中。现有第三方子类仍可覆盖原来的 `convert_document`；需要新格式时应使用公共入口或迁移到 native hook。
- 通用转换结果不能传给平台 `write_document`。

## Orchestration / Runtime layer

```python
from lazymind.document_tools import convert_document

result = convert_document('# 标题\n\n正文', output_format='latex')
latex_source = result['content']
```

`DocumentResourceToolkit.convert_document` / `WriterResourceToolkit.convert_document` 同样支持 `output_format`。工具层返回 JSON 字符串；共享函数返回 dict。

`builtin:document.convert_document.v1` 继续支持原有参数，新增可选的 `output_format` 和 `document`。后者是前端当前编辑快照，仅对通用格式有效，按内容解析，不按文件路径解析。

```json
{
  "action": "convert_document",
  "base_revision": 3,
  "input": {
    "output_format": "markdown",
    "document": "# 当前尚未保存的草稿"
  }
}
```

## Plugin layer

任意 workflow 都可以直接调用共享工具。对于通用格式，运行时为已校验的 workflow 包和 slot 提供内置转换 action，包括没有声明该 action 的旧版本包；native 转换仍按包原有 action 配置执行。

Writer workflow 的 `writer_convert_document` 新增 `output_format`：通用格式直接返回转换结果 JSON，native 保留原有转换产物文件路径返回值，兼容后续写回节点。

## Backend Core

Frontend 复用 `:action-preview`，保留现有会话归属、slot 和 revision 校验。通用转换不加载模型配置，不执行 `:action-execute` 的持久化逻辑，不产生新 revision 或修改 provider 同步状态。

## Frontend

共享文档工具栏以同一边框内的分段按钮显示复制操作和格式菜单，默认 Markdown，记住最近选择的格式。多个只读 Markdown 文件 slot 引用同一来源时，页脚只展示一套复制操作；可编辑 slot 保留独立操作和草稿快照。覆盖 WriterDocument、JSON/LMD 文档、Markdown 文件及 Markdown 文本 slot，按产物类型展示，不限制 workflow ID。

编辑器通过 `SlotEditingContext.registerSnapshot` 提供当前草稿。复制读取快照，不 flush 自动保存；Frontend 根据转换结果执行 `navigator.clipboard.writeText`。只有浏览器写入成功才提示“已复制”。失败时保留转换内容，允许用户再次点击复制或手动选取文本。

复制结果会移除正文中的编辑器章节锚点，将 `#block-*` 内部链接还原为显示文字，并去掉额外的段落空白行。LaTeX 用 `\par` 显式分段；Markdown 代码示例中的锚点文字和代码内部空行保留。native 写回格式不受这项清理影响。

## 第一版格式边界

- LaTeX 输出正文片段，使用现有工程的中文、字体等设置，不生成完整工程或 `.bib`。
- 图片保留地址引用；复制文本不会传输图片文件。LaTeX / 纯文本以说明文字和地址保留图片引用，需要用户自行整理图片。
- 支持正文、标题、强调、行内/块公式、代码、列表、表格和链接；平台独有且没有可移植正文的块明确报错。
- Markdown 是源码交付，目标平台是否将粘贴的源码自动渲染成排版取决于其编辑模式。
- 没有增加 clipboard 专用字段，也没有新增语雀或 Overleaf Provider。
