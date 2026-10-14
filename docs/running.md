# 运行参考

**中文** | [English](running.en.md) · [首页](../README.md)

本页集中记录环境、配置、运行和检查命令。项目目标与业务流程见[项目概览](../README.md)，状态、路由和恢复机制见[工作流设计](workflows.md)。

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

也可以复制 [config.example.toml](../config.example.toml)，通过 `--config config.toml` 设置独立生成器、校验器及 PIU 范围。配置优先级为 CLI > TOML > 环境默认值 > 内置默认值。

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

`--count` 表示计划任务数，拒绝任务不会补齐。扩写输入格式及字段限制见[数据契约](data.md)。自定义采样器通过 `--sampler` 指定，其结构见[采样设计](sampler.md)：

```bash
uv run midasdata generate --count 10 --seed 42 --sampler custom-sampler.json --out runs/sampler-check
```

## 恢复与产物

新运行使用尚不存在的输出目录，中断后使用原参数加 `--resume`：

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated --resume
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded --resume
```

恢复跳过已提交的通过和拒绝样本，重放未提交任务并复用保存的模型响应，保留技术和生成预算。可以改变并发数和预览页大小；其他配置、输入、采样器及提示词必须匹配。密钥值不进入指纹和配置快照。

运行文件及其职责见[运行产物](data.md#运行产物)。派生输出可以从已提交结果重建，原子替换以单个文件为单位。

直接用浏览器打开 `preview.html` 查看源文本、三档输出、失败原因和证据。预览离线可用，大运行按页显示，搜索和筛选作用于当前页。模型文本按文本渲染。

```bash
uv run python scripts/review_samples.py runs/expanded --out runs/expanded-review.csv
```

评审 CSV 保留空的人工判断字段，不自动批准样本。自动通过率不能代替模型质量评估和人工复核。

## 必要测试

离线测试覆盖关键流程、单档预算与盲校验边界、并行汇合、证据归属、模型与普通操作的执行恢复、技术重试、配置隔离、输入边界和导出。不测试固定提示词措辞，不调用远程模型，不读取项目数据集。

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
```
