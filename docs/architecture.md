# PairData 架构

PairData 通过配置定义 Source–Target 合成任务，固定工作流负责执行。每个通过任务导出一条 `{source, target}`，质量判断和运行过程记录到 Audit / Manifest。

## 三层结构

| 层 | 职责 | 技术 |
| --- | --- | --- |
| Configuration | 定义两个阶段的采样、提示词、输入、返回结构和通过条件 | YAML、Jinja2、JSON Schema |
| Runtime | 采样、渲染、模型调用、响应校验和规则解释 | Python、Pydantic、LangChain、jsonschema |
| Workflow | 固定节点与条件边，管理阶段状态、重生成、并发和导出 | LangGraph、asyncio |

Source 与 Target 都按 Sampler → Generate → Evaluate → Gate 执行。Source 通过后进入 Target；两个阶段均通过后导出。节点和路由保持异步，审计写入在同一事件循环内完成。

## 配置组织

```text
recipes/rewrite/          # 可直接运行的默认样例
  recipe.yaml            # 模型、阶段、输入绑定与运行参数
  samplers/
    source.yaml
    target.yaml
  prompts/               # 两阶段的生成、评估 Jinja2 模板
  schemas/               # 两阶段的生成、评估 JSON Schema
  rules/
    source.yaml
    target.yaml
examples/prompt_pairs/   # 多维采样与 PIU 评估示例，同样的目录结构
src/pairdata/            # 通用运行组件
```

recipe 目录自包含，所有资源路径相对 recipe.yaml。启动时校验全部配置与资源，用户可通过复制目录定义新任务。

## 执行边界

- 每阶段只采样一次。质量重生成保持 sample，Target 同时保持已通过的 Source。
- 两阶段可绑定 previous / feedback，读取上一轮候选、评估与程序判定；首次生成两者为 null。
- 技术重试复用同一逻辑请求的输入，与质量重生成分别计数。耗尽预算后拒绝任务，不补齐数量。
- LLM 提供内容和评估证据；程序通过 Schema 与固定规则操作符判定结构及质量。规则不执行任意代码。
- 断点恢复重放未提交任务，复用已落盘响应并保留请求预算。配置与资源指纹用于确认恢复的是同一任务。
- 分布统计区分已采样、阶段通过与最终导出，便于观察质量筛选造成的分布变化。

## 详细契约

- [配置与运行](running.md)：配置字段、输入绑定、环境和 CLI。
- [采样](sampler.md)：字段类型、权重、dropout 与确定性。
- [评估与决策](validation.md)：Schema 和规则操作符。
- [工作流](workflows.md)：节点、预算、并发与重放。
- [数据契约](data.md)：导出、运行产物和审计记录。
