# 评估与决策

**中文** | [English](validation.en.md) · [首页](../README.zh-CN.md)

LLM 给出生成内容或评估信息，程序验证响应结构并执行决策规则。评估响应不要求固定 passed 字段。

## JSON Schema

生成与评估都使用顶层为对象的 Draft 2020-12 JSON Schema。output_path 提取的 Source / Target 可为任意合法 JSON 值。支持同一 Schema 内的 `#/...` 引用；不加载外部 Schema。format 关键字不启用额外校验。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Evaluation",
  "type": "object",
  "properties": {
    "acceptable": {"type": "boolean"},
    "score": {"type": "number", "minimum": 0, "maximum": 1},
    "reason": {"type": "string"}
  },
  "required": ["acceptable", "score", "reason"],
  "additionalProperties": false
}
```

调用先保存原始响应，再解析 JSON、检查有限数值并验证 Schema。非法 JSON、Schema 不匹配、生成结果提取路径缺失使用当前逻辑请求的技术预算重试。Schema 定义错误在启动时停止。服务原生结构化输出与 JSON 文本模式均执行本地校验。

## 规则

```yaml
all:
  - field: evaluation.acceptable
    op: eq
    value: true
  - field: evaluation.score
    op: gte
    value: 0.8
  - field: candidate
    op: length_between
    min: 1
    max: 2000
```

| 操作符 | 行为 |
| --- | --- |
| all / any / not | 组合子规则；all / any 列表不能为空 |
| eq / ne | 同类型比较；整数和浮点数可互相比较，布尔值不当作数值 |
| gt / gte / lt / lte | 数值比较 |
| exists | 检查字段存在，null 也视为存在；不设置 value |
| in | 实际值是否在 value 数组中 |
| contains | 字符串包含子串，或数组包含指定 JSON 值 |
| length_between | 字符串字符数或数组元素数是否位于包含边界的区间 |

叶子规则使用 field/op；比较与内容规则需要 value，length_between 使用 min/max。value 可以是 JSON 常量或 `{ref: sample.threshold}`；长度边界可以是非负整数或引用。不执行 Python 或任意表达式。

Gate 可读取 sample、generation、candidate、evaluation；Target Gate 还可读取已通过的 source。Gate 的可见范围不会扩大 Target Generate 的输入范围。

运行时缺失字段（exists 除外）和类型错误使整次决策失败，即使它位于 not 或另一个分支已通过的 any 中，也不会被掩盖。比较不做字符串到数值等隐式转换。

决策保存 passed、reasons 和每条规则的 checks，包括位置、操作符、实际值、期望值及错误。合法响应但规则失败触发当前阶段重新生成。Source 和 Target 均可绑定 previous / feedback 使用上一轮候选、评估结果和程序判定；sample 保持不变，Target 的 source 也保持不变。技术重试则复用当前逻辑请求的输入。

## 示例任务的边界

rewrite 使用模型的 acceptable 和程序计算的字符长度。prompt_pairs 使用模型判断语义、有效性和新增 PIU 列表，程序检查布尔条件、问题列表为空、PIU 列表长度和代码围栏。Source 证据字符串由 contains 检查是否在原文中。

prompt_pairs 的逐项 PIU 证据和独立事实识别由模型评估；uniqueItems 只拒绝完全相同的 PIU 对象，不能识别语义重复。通用解释器不包含 PIU 分词或其他专用语义检查。
