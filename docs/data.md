# 数据契约与审计

**中文** | [English](data.en.md) · [首页](../README.zh-CN.md)

业务导出 `data.jsonl` 每行严格包含 source 和 target：

```json
{"source": "The meeting starts at 10.", "target": "Please join the meeting at 10."}
```

值来自各阶段生成响应的 output_path，类型由 recipe Schema 约束，可以是文本或结构化 JSON。业务导出不增加 ID、sample、level 或 messages。只有两个阶段通过的任务才导出一行，按计划任务顺序排列，不自动去重、补齐或划分训练集。

## 运行产物

| 文件 | 内容 |
| --- | --- |
| tasks.jsonl | 稳定 ID 和任务 index |
| results/<sha256(id)>.json | 原子提交的任务终态 |
| data.jsonl | 通过任务的 source-target 对 |
| lineage.jsonl | 任务 ID、状态及对应 data.jsonl 行号，行号从 1 开始 |
| rejected.jsonl | 拒绝结果及当前阶段详情 |
| audit.jsonl | 采样、请求、原始响应、成功解析、质量决策和技术错误 |
| recipe.json | 展开 sampler 后的配置与所有引用资源的文本快照 |
| manifest.json | 配置、指纹、状态、用量和分布统计 |
| run.lock | 操作系统运行目录锁 |
| preview.html、preview/ | 离线 Source / Target、采样和评估对照 |

TaskResult 包含 id、status、source、target 和 reason。source/target 的 StageResult 保存 sample、generation_attempt、generation、candidate、evaluation、decision、status、reason。尚未进入 Target 时 target 为 null。任务状态为 passed/rejected；阶段状态为 pending/passed/quality_failed/technical_failed。

动态 generation/evaluation 使用 recipe Schema 校验；固定内部记录使用 Pydantic。只有两个阶段状态及 decision 都通过，才允许提交通过任务。

## 记录与恢复

OperationEvent 包含 run_id、sample_id、stage、logical_call_id、generation_attempt、technical_attempt、model、timestamp、event 和 data。阶段名形如 source.sample、target.generate、target.evaluate、target.gate。

请求载荷保存显式 inputs、实际 messages 和 response schema；响应在校验前落盘。Source / Target Generate 均可配置 previous / feedback 绑定；Target 另外可绑定已通过的 source，自身 sample 在重生成时保持不变。

质量决策包含 passed、reasons、checks。每条规则记录路径、操作符、实际值、期望值、通过状态及可选错误。采样和质量决策没有模型调用用量；token 统计只累计响应中实际返回的整数用量。

追加审计后 flush/fsync；文件替换使用临时文件、fsync、原子替换和目录 fsync。原子性以单个文件为单位，恢复会重建派生导出。

API 密钥通过环境变量读取，不进入配置快照或指纹。规则与模型输出仍可能包含用户提供的业务内容，Audit 保存这些内容用于追踪。

内部审计对象使用 `task_id`、`operation` 和 `request_attempt`；落盘通过序列化别名继续使用 `sample_id`、`stage` 和 `technical_attempt`，已有审计日志仍可读取。
