# 配置与运行

**中文** | [English](running.en.md) · [首页](../README.zh-CN.md)

在项目根目录运行时，默认读取 `recipes/rewrite/recipe.yaml`；复杂示例位于 `examples/prompt_pairs/`，通过 `--recipe` 选择。复制整个 recipe 目录后修改文件即可定义新任务。`check` 验证固定配置、sampler、模板变量、Schema 与规则结构，不调用模型；它不验证服务凭据，也不能提前判断所有运行时数据路径。

```bash
uv run pairdata check
uv run pairdata run --count 20 --seed 42 --out runs/rewrite
```

## Recipe 顶层

```yaml
name: my_recipe
models:
  generator:
    temperature: 0.8
  evaluator:
    temperature: 0.0
runtime:
  concurrency: 4
  timeout: 120.0
  max_request_attempts: 5
  retry_initial: 1.0
  retry_max: 30.0
  preview_page_size: 100
```

`source` 和 `target` 阶段为必填。每个阶段使用自己的 sampler 文件、生成调用、评估调用、decision 和生成预算。sampler 文件内容可为 `{}`；max_generations 默认 3，max_request_attempts 默认 5，都包含第一次。

模型配置支持 `model`、`base_url`、`api_key_env`、`temperature`、`structured_output`（`json` / `native`）、`reasoning_effort`、`extra_body`。未指定 model/base_url 时读取 `MODEL_NAME` / `OPENAI_BASE_URL`。模型配置可使用 `${ENV_NAME}` 插值；凭据由 `api_key_env` 指定的环境变量提供，默认 `OPENAI_API_KEY`。

服务专有参数通过 `extra_body` 显式设置，运行时不会根据服务域名覆盖模型选项。

环境优先级：进程环境 > `--env-file`（默认 `.env`）；模型配置覆盖环境默认值；CLI 的并发、超时和预览分页覆盖 recipe.runtime。环境文件按文本解析，不执行 shell，不修改进程环境。

## 阶段配置

```yaml
target:
  sampler: samplers/target.yaml
  max_generations: 3
  generate:
    model: generator
    inputs:
      source: {ref: source}
      sample: {ref: sample}
      previous: {ref: previous}
      feedback: {ref: feedback}
    messages:
      - role: user
        template: prompts/target_generate.j2
    response_schema: schemas/target_generate.json
    output_path: text
  evaluate:
    model: evaluator
    inputs:
      source: {ref: source}
      target: {ref: candidate}
      sample: {ref: sample}
    messages:
      - role: user
        template: prompts/target_evaluate.j2
    response_schema: schemas/target_evaluate.json
  decision: rules/target.yaml
```

Source 同样指定 `sampler: samplers/source.yaml`。`samplers/target.yaml` 独立定义采样字段：

```yaml
tone:
  type: categorical
  choices:
    - value: friendly
    - value: formal
```

生成和评估都可以配置 `system` / `user` 消息列表。每条消息指定一个模板文件；运行时附加响应 JSON Schema 指令。sampler、模板、Schema、规则文件路径相对 recipe.yaml，必须位于其目录内。不支持模板 include/import/extends，也禁用 random 过滤器，随机性由 sampler 管理。

`inputs` 的值可以是 JSON 常量，或完整形式的 `{ref: field.path}`。引用只在输入绑定的顶层解析。路径支持 `sample.tone`、`generation.items[0].text`；output_path 还可使用 `$` 提取整个响应对象。

| 调用 | 可引用的数据 |
| --- | --- |
| Source generate | `sample`、`previous`、`feedback` |
| Source evaluate | `sample`、`generation`、`candidate` |
| Target generate | `source`、`sample`、`previous`、`feedback` |
| Target evaluate | `source`、`sample`、`generation`、`candidate` |

`generation` 是当前完整生成响应，`candidate` 是 output_path 提取的值。两个阶段首次生成的 previous/feedback 均为 null；重生成时 previous 是该阶段上一轮候选，feedback 包含上一轮 evaluation/decision。recipe 可选择绑定并在模板中使用。Target sample 是 Target 自己的采样，source 是已通过的候选内容；Source 的采样与评估记录不在 Target 输入上下文中。

模板只看到 inputs 中的变量。未定义变量会报错；可选采样字段请在 Jinja2 中使用 `default` 或 `is defined`。模板引用 JSON 对象、数组时可用 `tojson`，例如 `{{ source | tojson }}`。

## 恢复

```bash
uv run pairdata run --recipe recipes/rewrite/recipe.yaml --count 20 --seed 42 --out runs/rewrite --resume
```

新运行要求输出目录不存在。恢复要求请求、模型配置、sampler、模板、Schema 和规则一致，仅并发与预览分页可以改变。API 密钥值不进入指纹。recipe.json 保存展开 sampler 后的配置与资源内容快照；恢复时重新加载提供的 recipe 并检查指纹。

`--count` 为计划任务数，不保证最终通过数。拒绝任务不会自动补齐。`--concurrency` 控制整个运行中的在途模型请求数。

```bash
uv run python scripts/review_samples.py runs/rewrite --out runs/rewrite-review.csv
```

CSV 提供空白人工评审栏。预览在浏览器中离线打开；搜索与状态筛选只作用于当前页。
