# 采样

**中文** | [English](sampler.en.md) · [首页](../README.md)

Source 和 Target 分别使用 `samplers/source.yaml` 和 `samplers/target.yaml`。在 recipe.yaml 的阶段配置中使用 `sampler: samplers/source.yaml` 或 `sampler: samplers/target.yaml` 引用文件。字段独立采样，每个阶段首次生成前采样一次并记录；Target 在 Source 通过后采样。

每个 sampler 文件直接声明字段，例如：

```yaml
language:
  type: categorical
  dropout: 0.1
  choices:
    - value: zh
      weight: 3.0
    - value: en
      weight: 1.0
limit:
  type: integer
  min: 10
  max: 30
```

`categorical` 按权重选择一个 JSON 值，权重默认为 1。权重必须有限且非负，总和必须为正，程序归一化；候选值不得重复。value 可以是字符串、数值、布尔值、null、数组或对象。

`integer` 从包含上下界的整数区间均匀采样。每个字段都有可选的 dropout，范围 0–1，默认 0。丢弃的字段不会出现在 sample 中；sampler 文件内容为 `{}` 时产生空对象。字段名使用字母或下划线开头的简单标识符。文件在启动时校验，并纳入配置快照与恢复指纹。

确定性随机种子来自用户 seed、稳定任务序号、阶段与字段名；不依赖完成顺序或字段声明顺序。运行 ID 仍随机生成。固定 seed 复现采样，不保证模型措辞可复现。

质量重生成、技术重试和恢复都保留原 sample。Source 被拒绝时，该任务不会产生 Target sample。

Manifest 的 distributions 对每阶段记录 sampled_tasks、not_reached，以及 sampled / stage_passed / exported 三种分布。值使用 JSON 编码作为统计键；缺失字段记为 `(dropout)`。未进入阶段的任务单独计数，不记为字段 dropout。

count 表示计划任务数；拒绝不补齐。质量过滤会改变最终分布，通过上述统计提供可见性，不做配额补齐或自动调权。

`examples/prompt_pairs/` 使用五维 Source 分布及 0.1 dropout。Target 的 expansion 字段选出一个包含 target/min/max 的对象：目标为 0、2–4 或 5–8；三个区间的总权重相等，区间内各目标等权。这些都是 recipe 数据，运行时没有固定档位。
