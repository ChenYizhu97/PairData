# 五维采样

**中文** | [English](sampler.en.md) · [首页](../README.md)

默认采样器随包提供，位于 `src/midasdata/sampler.json`，以包资源方式加载，无需在工作目录中放置采样器文件。选项解释是提供给模型的英文输入数据，会原样传递。

## 维度与默认值

| 维度 | 选项与权重 |
| --- | --- |
| `domain` | `people` 0.20, `animals` 0.15, `objects` 0.15, `scenes` 0.15, `plants_food` 0.10, `vehicles` 0.10, `art_design` 0.10, `cultural_abstract` 0.05 |
| `focus` | `subject` 0.20, `action` 0.20, `scene` 0.15, `relation` 0.15, `composition` 0.15, `aesthetic` 0.15 |
| `specificity` | `minimal` 0.40, `moderate` 0.60 |
| `expression` | `keywords` 0.35, `natural` 0.40, `colloquial` 0.25 |
| `language` | `zh` 0.75, `en` 0.15, `mixed` 0.10 |

每个维度独立以 0.1 的概率被丢弃。保留时，按该维度的权重选择选项。所有维度都可能被丢弃，产生 `{}`。CLI 使用固定的丢弃概率，不提供覆盖参数。

`expression` 控制关键词、自然句子或口语化请求。源描述以中文为主：保留语言维度时，默认语言权重为中文 75%、夹杂英文视觉关键词的中文 10%、英文 15%。这些选择由 `language` 独立控制。`mixed` 是语言选项，不是表达方式选项。语言维度被丢弃时不限制语言，校验器仍检查视觉内容和内容是否干净。

`generate --seed 42` 使用独立的 `random.Random`。任务按维度顺序（`domain`、`focus`、`specificity`、`expression`、`language`）采样，并在 worker 启动前写入 `tasks.jsonl`，因此响应顺序、技术重试和质量修订不会改变采样序列。质量修订保留任务 ID 和采样条件。

被丢弃的维度不作为条件检查。`specificity` 控制源描述明确给出的视觉信息，扩写 `level` 控制英文提示词新增的视觉信息，两者独立。`minimal` 围绕一个主要主体或场景，只指定少量细节；`moderate` 包含若干具体视觉属性、关系或约束。两者都应简洁自然，不追求冗长或穷尽式描述。

固定种子能复现采样，不能复现模型措辞或随机分配的运行 ID。原始响应保存在审计日志中。`--count` 表示计划任务数；拒绝不会触发补充采样。运行清单统计五个维度的计划分布和通过分布，缺失维度标为 `(dropout)`。没有采样元数据的扩写记录，在这些统计中也归入 `(dropout)`。

## 自定义采样器

```bash
uv run midasdata generate --count 10 --seed 42 --sampler custom-sampler.json --out runs/sampler-check
```

自定义文件必须恰好包含五个维度键，每个维度的选项列表不能为空。每行结构为 `[option, explanation, weight]`。例如：

```json
{
  "domain": [["animals", "Animals and creatures", 1.0]],
  "focus": [["subject", "Focus on the subject", 1.0]],
  "specificity": [["minimal", "One main visual idea", 1.0]],
  "expression": [["natural", "Natural-language description", 1.0]],
  "language": [["en", "User descriptions written entirely in English", 1.0]]
}
```

启动校验要求选项字符串受支持且不重复、解释字符串不为空白、权重为有限的非负数。布尔值不能作为权重，每个维度的权重之和必须在绝对误差 `1e-9` 内等于 1。允许零权重选项。

自定义采样器可以修改权重或选择现有选项的子集，不能添加新维度或枚举值。`null` 不是合法采样选项。维度缺失只由丢弃产生，在采样字典中表现为省略该维度。修改采样器的任何内容（包括解释）都需要新建运行，因为整个采样器参与恢复指纹计算。
