# 数据契约与来源追踪

**中文** | [English](data.en.md) · [首页](../README.md)

JSONL 文件使用 UTF-8 编码，每个非空行包含一个 JSON 对象。业务记录模型拒绝未知字段、空白 ID 或提示词，以及不支持的扩写等级。ID 和提示词会去除首尾空白。

## 业务记录

源描述生成导出：

```json
{"id":"gen:run001:000001","sample":{"domain":"animals"},"prompt":"一只橘猫趴在窗边"}
```

这个示例描述了一只趴在窗边的橘猫。`sample` 支持 `domain`、`focus`、`specificity`、`expression` 和 `language`；其中 specificity 为 `minimal | moderate`，language 为 `zh | en | mixed`，expression 为 `keywords | natural | colloquial`。源描述以中文为主，也包含部分夹杂英文关键词的中文和纯英文。被丢弃的维度不出现在 `sample` 中；`{}` 是合法值。选定的视觉概念保存在内部结果、审计响应和来源追踪记录中，不属于业务记录。生成的 ID 包含运行 ID 和稳定的任务序号。

扩写接受以下外部输入：

```json
{"id":"gen:run001:000001","prompt":"一只橘猫趴在窗边"}
```

CLI 接受这种 `InputPrompt` 结构，也接受带有 `sample` 的 `SourcePromptRecord`。它先校验输入，将接受的完整结构保存到任务列表，再提取 `task_id` 和 `source_prompt` 调用工作流。未提供 `sample` 的源记录没有采样元数据；runner 仍会将解析后的输入路径记录为来源。去除首尾空白后，输入 ID 必须唯一。不接受任意附加的输入元数据。

扩写导出：

```json
{"id":"gen:run001:000001","level":"light","prompt":"An orange cat lying by a window."}
```

`level` 为 `light | medium | rich`，`(id, level)` 唯一标识一条扩写记录。只有整组通过才导出三行。组内按 Light、Medium、Rich 排列，各组按输入顺序排列。

`sft.jsonl` 使用 `prompts.py` 中定义的模板。每行包含一个 `messages` 数组，包含三个角色：system 描述扩写任务，user 提供 `Expansion level: <level>\nSource description: <source>`，assistant 包含通过校验的英文提示词。system 和 user 中的任务指令使用英文。模板采用不限定语言的标签 `Source description:`，源文本按接收到的内容保留。SFT 行不单独设置 ID 和等级字段；等级包含在 user 消息中。业务导出和 SFT 导出来自同一批已提交结果。不自动划分训练集和验证集，也不自动去重或合并。

## 运行产物

| 路径 | 内容 |
| --- | --- |
| `tasks.jsonl` | 在模型请求前保存的稳定、已校验任务序列 |
| `results/<sha256(id)>.json` | 每个样本原子提交的完整终态 `TaskResult`；扩写结果包含整组 |
| `data.jsonl` | 仅包含通过样本的业务记录 |
| `sft.jsonl` | 仅包含通过扩写组的训练消息 |
| `sources.json` | 来源类型、请求或输入摘要、采样器及软件版本 |
| `lineage.jsonl` | 每个已提交样本的原始任务元数据、内部概念和终态 |
| `audit.jsonl` | 请求尝试、原始响应与用量、成功通过解析检查的输出快照、节点质量决策和错误；在第一个事件产生时创建 |
| `rejected.jsonl` | 完整的拒绝结果，包含原因和可用的中间细节 |
| `manifest.json` | 不含 API 密钥值的配置、请求/任务/提示词摘要、软件版本、运行状态和导出统计 |
| `run.lock` | 运行目录锁文件；存储关闭时释放操作系统锁，文件保留 |
| `preview.html`, `preview/page-*.html` | 可独立打开的浏览器预览，以及按需生成的完成样本分页 |

审计事件区分 `technical_attempt`（一次逻辑模型操作内最多五次）与 `generation_attempt`（每条源描述或每个英文扩写等级最多三次）。`logical_call_id` 由样本 ID、阶段、等级、轮次和生成次数确定。每次请求保存输入载荷摘要，以检测重放是否一致。token 统计汇总服务返回的整数用量字段；服务未返回用量时保持为空，不估算费用。

runner、执行层和工作流节点在事件循环中同步通过 `RunStore` 写入。每次追加审计事件都会 flush 和 fsync；模型请求开始前，请求事件已经持久化。任务文件、运行清单、来源元数据、已提交结果、导出和预览使用临时文件、fsync、原子替换和目录 fsync。原子替换以单个文件为单位，不覆盖所有导出文件；中断后恢复会重建导出。`run.lock` 阻止两个进程同时写入同一个运行目录。

## 内部结果结构

`TaskResult.workflow` 决定记录和详情的类型：

| 工作流 | `records` | `details` |
| --- | --- | --- |
| `generate` | `SourcePromptRecord` 列表 | `SourceGenerationDetails`：草稿 `prompt`、当前 `validation` 和 `decision`、`generation_attempt`、`feedback` |
| `expand` | `ExpandedPromptRecord` 列表 | `PromptExpansionDetails`：`round`、`levels`、当前 `group_validation` 和 `group_decision`、各等级的 `feedback` |

`PromptExpansionDetails.levels` 中的每一项都是 `ExpansionResult`，包含等级、轮次、生成次数、状态、提示词、可选的校验报告和决策，以及原因。它的状态为 `passed`、`quality_failed` 或 `technical_failed`。这些是单档状态；外层样本的状态为 `passed` 或 `rejected`。

不属于当前工作流的记录和详情，以及嵌套结构中的未知字段，都会被拒绝。`source` 允许扩展来源元数据。存储、导出和评审脚本读取有类型约束的模型；预览生成时将它们序列化为嵌入页面的 JSON。

可选详情字段连同默认值一起完整序列化，包括缺少校验报告和决策时的 `null`。只有业务记录的 `sample` 会省略被丢弃的维度。决策中的 `insufficient_expansion_space` 标记与原始校验报告、拒绝原因一起保存。产物按 `schemas.py` 中的当前契约读写。

新草稿替换旧文本后，当前校验报告和决策会清空，直到新草稿完成校验。先前的证据仍保存在 `audit.jsonl` 中。进入新一轮扩写时，也会清空上一轮的组校验报告和决策，同时将修订反馈传给需要修改的等级。

软件版本和工作流版本包含在运行清单及恢复指纹中。内部图状态与工作流节点定义在同一模块，不放在 `schemas.py` 中。
