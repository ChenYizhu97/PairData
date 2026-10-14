# MidasData

**中文** | [English](README.en.md)

MidasData 生成以中文为主的视觉源描述，再将每条描述独立扩写为 Light、Medium、Rich 三档英文图像提示词。只有整组三档通过校验才导出训练数据。生成与扩写可以分别运行，模型请求和质量判断可审计，中断后可以恢复。

## 安装与运行

需要 Python 3.11+、uv 和支持文件锁及目录 fsync 的 POSIX 环境。

```bash
uv sync --locked
uv run midasdata generate --help
uv run midasdata expand --help
```

在工作目录的 `.env` 中配置模型：

```dotenv
OPENAI_BASE_URL=https://your-service.example/v1
MODEL_NAME=your-model
OPENAI_API_KEY=your-secret
```

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded
```

也可以复制 `config.example.toml`，通过 `--config config.toml` 设置独立生成器、校验器及 PIU 范围。配置优先级为 CLI > TOML > 环境默认值 > 内置默认值。

环境变量使用 `OPENAI_BASE_URL`、`MODEL_NAME`、`OPENAI_API_KEY`。进程环境优先于 `.env`。`.env` 按文本解析，配置加载不修改进程环境。`--env-file` 可以指定其他文件。TOML 的 `${ENV_NAME}` 支持嵌套插值。

`--model` 只覆盖生成器，`--validator-model` 只覆盖校验器；`--base-url` 和 `--structured-output` 覆盖两个角色。模型配置支持 `temperature`、`reasoning_effort`、`extra_body` 和 `api_key_env`。DeepSeek 的两个角色默认关闭 thinking，显式设置优先。

| 配置 | 默认值 |
| --- | --- |
| `concurrency` | 4 个同时在途的模型请求 |
| `timeout` | 每请求 120 秒 |
| `preview_page_size` | 每页 100 个完成样本 |
| `retry_initial` / `retry_max` | 退避 1 / 30 秒 |
| 新增 PIU 范围 | Light 0；Medium 2–4；Rich 5–8 |

`structured_output="json"` 解析模型 JSON 文本，`"native"` 使用服务的 JSON Schema 接口。两种模式执行相同响应契约。服务不支持的接口等非重试 HTTP 错误会停止运行。

采样器和提示词定义在 `sampler.py`、`sampler.json`、`prompts.py`。采样使用 domain、focus、specificity、expression、language 五个独立维度，在并发请求前保存任务。拒绝任务不会补齐；`--count` 表示计划任务数。扩写输入接受 `{ "id": "sample-1", "prompt": "一只橘猫趴在窗边" }`，也接受生成输出的 `sample` 字段；未知字段和重复 ID 会在模型请求前被拒绝。

## 代码阅读顺序

先看三个工作流，每个文件按 **状态 → 节点 → 路由 → build** 排列：

| 文件 | 职责 |
| --- | --- |
| [source_generation.py](src/midasdata/workflows/source_generation.py) | 源描述生成、校验、修订与终止 |
| [expansion_attempt.py](src/midasdata/workflows/expansion_attempt.py) | 指定档位的一次生成与校验 |
| [prompt_expansion.py](src/midasdata/workflows/prompt_expansion.py) | 三档并行、共享预算、定向修订与组校验 |

扩写等级统一使用 `level`（Light、Medium、Rich），类型为 `ExpansionLevel`。`expansion_attempt` 表示指定等级的一次生成与校验。

主调用链为 **runner → 工作流节点 → execution.execute → LLMModel**：

| 模块 | 职责 |
| --- | --- |
| `runner.py` | 创建模型与共享执行资源，调度样本、提交结果、导出 |
| `workflows/` | 准备提示词、执行操作、根据证据计算质量结果、更新状态和路由 |
| `execution.py` | 普通异步操作的历史复用、并发限制、技术重试与记录 |
| `model.py` | `LLMModel` 适配模型接口，发送消息并解析响应 |
| `storage.py` | `RunStore` 保存运行配置、任务、操作历史、质量决策和结果 |

`ExecutionContext` 只保存共享存储、并发信号量和退避参数。`LLMModel` 不持有运行存储或工作流状态。质量结果由节点调用 `validation.py` 中的纯函数计算，再交给路由处理；执行层只返回解析后的操作输出。普通状态计算和分支选择直接在工作流中完成。

其余模块各处理一件事：`schemas.py` 定义响应和落盘契约；`planning.py` 规划任务；`exports.py` 导出；`preview.py` 生成离线预览。CLI 只处理参数和结果报告。

节点不保存样本级对象状态，也不直接导出文件。单次扩写子图没有质量修订循环，所有修订由父图管理，单档与组修订共享每档三次生成预算。执行层使用显式技术重试循环。模型操作最多五次请求尝试，包括第一次；其他操作由调用方指定重试规则，默认只执行一次。

完整流程见 [工作流与恢复](docs/workflows.md)，数据和质量规则见 [数据契约](docs/data.md)、[采样](docs/sampler.md)、[校验](docs/validation.md)。

## 恢复与产物

新运行使用尚不存在的输出目录，中断后使用原参数加 `--resume`：

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated --resume
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded --resume
```

恢复跳过已提交的通过和拒绝样本，重放未提交任务并复用保存的模型响应，保留技术和生成预算。可以改变并发数和预览页大小；其他配置、输入、采样器及提示词必须匹配。密钥值不进入指纹和配置快照。

运行目录包含 `tasks.jsonl`、`sources.json`、`manifest.json`、`results/`、`audit.jsonl`、`data.jsonl`、`lineage.jsonl`、`rejected.jsonl`、`run.lock` 和 `preview.html`；扩写还导出 `sft.jsonl`。零任务运行不需要审计文件。派生输出可以从已提交结果重建，原子替换以单个文件为单位。

直接用浏览器打开 `preview.html` 查看源文本、三档输出、失败原因和证据。预览离线可用，大运行按页显示，搜索和筛选作用于当前页。模型文本按文本渲染。

```bash
uv run python scripts/review_samples.py runs/expanded --out runs/expanded-review.csv
```

评审 CSV 保留空的人工判断字段，不自动批准样本。自动通过率不能代替模型质量评估和人工复核。

## 必要测试

离线测试覆盖关键流程、共享预算、并行汇合、证据归属、模型与普通操作的执行恢复、技术重试、配置隔离、输入边界和导出。不测试固定提示词措辞，不调用远程模型，不读取项目数据集。

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
```
