# Paper-writing 真实模型测试

`test_paper_writing_live.py` 通过 `api.run_algorithm` 调用当前 `write_outline`、`write_document` 和 `revise_document`，实际经过 LazyMind 的 WriterCreateToolkit / WriterRevisionToolkit、模型规划／生成／质量检查及公共替换应用。没有替换模型响应或 HTTP 请求，不修改 LazyLLM，不经过 Workflow 调度与前端。

默认收集时跳过全部 5 个场景，只有 `PAPER_WRITING_LIVE=1` 才调用服务。启用后，配置缺失、服务错误、质量拒绝或断言失败均视为失败，不转成 skip、不回退演示数据。运行会产生模型调用费用。

## 模型配置

二选一：

1. 使用已有静态模型角色配置：设置 `LAZYMIND_MODEL_CONFIG_PATH` 为已配置文件的绝对路径，或已有 `inner` / `online` 别名，按对应供应商方式配置凭据。测试沿用该文件的 `llm` 角色，不硬编码模型。
2. 使用与平台请求相同的动态配置：设置 `PAPER_WRITING_LIVE_CONFIG` 为仓库外 JSON 文件的绝对路径。结构为 `{"llm": {"source": "供应商标识", "model": "模型名", "base_url": "服务地址", "api_key": "凭据"}}`。`base_url` 按供应商需要提供；无鉴权的受控服务可使用 `skip_auth: true`。文件内容必须是实际配置，不自动展开环境变量。测试通过 `inject_model_config` 注入 llm 角色，并临时选择 dynamic 角色配置；结束时恢复配置。

动态配置文件不放入仓库，不写入测试产物。不要使用 pytest 的 `--showlocals` 输出包含凭据的局部变量。

## 使用公共 Writer 收费测试的模型调用方式

设置 `PAPER_WRITING_LIVE_PROFILE=public-writer`，使用与 LazyLLM `test_writer_pipeline.py` 相同的真实构造：`OnlineChatModule(source='qwen', model='qwen-turbo', api_key=lazyllm.config['qwen_api_key'], stream=False)`。公共测试的 get_api_key 也只是读取这一配置项，没有单独的测试 Key。

此模式仅在测试中将 LazyMind writing/revision 的模型工厂指向上述真实模型，结束后恢复。Toolkit 编排、模型响应、HTTP、修改应用和检查均保持真实，不修改生产代码或 LazyLLM，不代表用户级 dynamic 配置已联调。

```bash
PAPER_WRITING_LIVE_PROFILE=public-writer PAPER_WRITING_LIVE=1 \
  LAZYLLM_TRACE_ENABLED=false PYTHONPATH=algorithm/lazyllm:algorithm \
  .venv/bin/dotenv -f .env run --no-override -- \
  .venv/bin/python -m pytest \
  tests/algorithm/chat/workflows/test_paper_writing_live.py -k review_revision -x -v
```

该命令仅为测试进程关闭追踪上报，沿用项目已有 Qwen 凭据。仍需有效服务端鉴权与网络访问许可；模型构造方式改变不会修复无效凭据。

可通过 `PAPER_WRITING_LIVE_SOURCE=minimax` 切换到 MiniMax，并自动读取 `lazyllm.config['minimax_api_key']`（对应 `.env` 的 `LAZYLLM_MINIMAX_API_KEY`）。可选 `PAPER_WRITING_LIVE_MODEL` 指定模型；未指定时，Qwen 保持公共测试的 qwen-turbo，其他供应商使用其现有适配器默认模型。当前 MiniMax 默认模型为 MiniMax-M3、默认服务为 api.minimax.io；这不是自动读取用户级 dynamic 模型选择，也不是 inner 中的私有兼容服务。

## 运行

在仓库根目录执行，先通过环境配置上述任一路径：

```bash
PAPER_WRITING_LIVE=1 PYTHONPATH=algorithm/lazyllm:algorithm \
  .venv/bin/python -m pytest \
  tests/algorithm/chat/workflows/test_paper_writing_live.py -v
```

静态配置测试可加载项目根目录 `.env` 的供应商凭据。例如沿用仓库 `runtime_models.online.yaml` 中的 llm 角色：

```bash
LAZYMIND_MODEL_CONFIG_PATH=online PAPER_WRITING_LIVE=1 \
  PYTHONPATH=algorithm/lazyllm:algorithm \
  .venv/bin/dotenv -f .env run --no-override -- \
  .venv/bin/python -m pytest \
  tests/algorithm/chat/workflows/test_paper_writing_live.py -v
```

`--no-override` 保留命令行指定的 online 配置，避免 `.env` 中的 dynamic 值覆盖它。直接运行 pytest 不会自动加载 `.env`；dynamic 还需要请求级 llm 配置注入，仅有供应商密钥不足以选定模型。这里显式选择已有 online 配置，不修改项目默认启动配置。注意：online/inner 静态角色不是界面中的用户级 dynamic 模型选择，不能用它们的结果代替用户配置验收。用户级选择由 Core 按 userID 读取并解析为 llm_config 后注入；当前独立 pytest 尚未自动接入这一加载链路。

仅验收审稿修订时追加 `-k review_revision`；首次可追加 `-x`，遇到第一个错误即停止。

## 场景与验收边界

- 无大纲材料写作、给定大纲写作：检查完整 Markdown、标题与大纲一致、材料中的 15 分差异和单次实验局限。
- draft 已有稿件修改：要求精确的局部替换，检查完整稿件与预期一致。
- revise 仅选 R1：输入同时包含 R1、R2，只落实 R1，检查未选择意见及无关内容保持原样。
- revise 选中 R1、R2：检查两条修改均落实，完整稿件与预期一致。

所有场景检查输入不变及 `is_demo=false`；revise 检查旧大纲原样返回。生成内容不完全确定，测试不自动放宽断言或重试以隐藏失败。测试输入为合成的小型研究材料，不能证明长稿、中英文篇幅、复杂结构修订、事实真实性或主 Workflow 已验收。

每个场景在 pytest 的 `tmp_path` 保存 `input.json`，调用成功后保存 `output.json` 与 `manuscript.md`，即使后续断言失败也保留。模型调用失败时仅有输入。pytest 的 `paper_writing_artifacts` 测试属性记录目录；需要机器可读记录时可附加 `--junitxml=/tmp/paper-writing-live.xml -o junit_family=legacy`。公共 Toolkit 的中间产物仍按其既有受管临时目录机制保存，不由测试替换。

人工复核时应阅读完整成稿、证据边界及修改差异；“测试代码已添加”“真实模型执行通过”和“内容质量验收通过”分别记录。

## MiniMax 调用配置

同一 public-writer 模式支持 `PAPER_WRITING_LIVE_SOURCE`、`PAPER_WRITING_LIVE_MODEL` 和 `PAPER_WRITING_LIVE_BASE_URL`。指定 source=minimax 时从 LazyLLM 配置读取 minimax_api_key，使用项目 `.env` 的 `LAZYLLM_MINIMAX_API_KEY`；不打印或复制密钥。

2026-10-19 按用户指定的 `minimax-2.7max-highspeed` 与 `https://api.minimaxi.com/v1/` 运行单选审稿测试，服务返回 HTTP 400。向同一地址发送最小诊断请求确认错误为 `unknown model 'minimax-2.7max-highspeed' (2013)`。因此该模型 ID 尚不可用，不能将本次视为真实修订验收通过，也不自动替换模型。

用户随后确认模型为 `MiniMax-M2.7-highspeed`，该模型已成功返回响应。使用以下配置运行（无需复制或改写 Key）：

```bash
PAPER_WRITING_LIVE_PROFILE=public-writer PAPER_WRITING_LIVE_SOURCE=minimax \
  PAPER_WRITING_LIVE_MODEL=MiniMax-M2.7-highspeed \
  PAPER_WRITING_LIVE_BASE_URL=https://api.minimaxi.com/v1/ \
  PAPER_WRITING_LIVE=1 LAZYLLM_TRACE_ENABLED=false \
  PYTHONPATH=algorithm/lazyllm:algorithm \
  .venv/bin/dotenv -f .env run --no-override -- \
  .venv/bin/python -m pytest \
  tests/algorithm/chat/workflows/test_paper_writing_live.py -v
```

单选审稿首次真实执行发现公共 paragraph 定位不支持标题紧接正文的样例格式；写作侧已提示精确替换使用 fragment，未放宽范围检查或测试断言。修改后单选真实测试通过（1 passed, 4 deselected）。其余场景结果见实现文档。

最新真实运行：指定 MiniMax 模型下，单选／多选审稿、给定大纲写作及已有稿件修改均已通过；无大纲写作因生成大纲跳过 H2 被校验拒绝，仍失败。这是多次针对性运行后的逐场景结果，不是全套一次通过。具体次数、修复和限制见 F5-IMPLEMENT.md。

适配说明（2026-11-04）：新稿没有大纲时先调用 write_outline，再调用 write_document；已有稿件统一调用 revise_document。正文接口只返回 manuscript/is_demo，大纲由独立步骤持有。审稿修订同时携带 review Markdown 与 review_data，并在修改意见后重新计算对应摘要。
