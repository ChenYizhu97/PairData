# 校验规则

**中文** | [English](validation.en.md) · [首页](../README.md)

模型承担语义判断与新增事实识别，程序承担可以确定计算的规则。扩写是否通过，取决于盲校验语义结论和程序检查同时通过；整组接受由 `group_check` 决定。业务流程见[项目概览](../README.md)。

## blind_sampler_validation：源描述校验

源描述校验器（`blind_sampler_validation` / `SourceValidation`）只评估实际采样的维度，为每个维度返回条件结果和证据。程序要求条件键与采样维度完全一致、`satisfied=true`，且证据是描述中连续出现的原文片段。同时要求 `visual=true`、`visual_evidence` 能在原文中定位，以及 `clean=true`。描述不能包含 Markdown 代码围栏。`language=zh` 或 `mixed` 时，必须包含汉字；`mixed` 还必须包含拉丁字母。`language=en` 时，必须包含拉丁字母，且不能包含汉字。语言维度被丢弃时，不施加字符层面的语言限制。语言占比、是否为实际英文表达，以及混合语言词语是否描述视觉内容，仍由模型判断。

提示词按 `expression` 允许关键词、自然语言和口语化图像请求；中文、英文和中英混合由 `language` 独立控制。例如，`帮我画一只狗` 是图像请求，`养狗好吗` 则是在询问养狗是否合适。校验指令要求拒绝无关对话、乱码、解释性内容和照抄的采样标签。混合语言中的词语必须描述视觉内容。

对于 `art_design`，校验指令要求明确的设计形式，例如插画、图标、标志、海报、平面设计、包装或字体设计。只有绘画请求和主体还不够，证据必须引用所指定的设计形式。这是一条由模型评估的语义规则；程序检查返回的条件结果和证据位置，不另设设计关键词分类器。

条件不满足和证据未找到分别记录为 `condition_not_satisfied:<dimension>` 和 `condition_evidence_not_found:<dimension>`。

## Expand：按抽取的目标数量生成

Python 为每个档位从其闭区间内均匀抽取一个整数。生成器每次只接收 `source` 和固定目标数量 `target_added_pius`。

| 内部等级 | 默认新增 PIU 范围 |
| --- | --- |
| Light | 0 |
| Medium | 2–4 |
| Rich | 5–8 |

Light 固定为零；Medium 和 Rich 可配置，范围必须严格递增且互不重叠。源事实与约束优先于新增目标，不能为了凑数改变数量、否定、关系、风格或要求出现在图像中的文字。

目标按运行 ID、任务 ID 和 level 确定随机种子。每档在工作流初始化时抽取一次，重新生成、技术重试与断点恢复都保持目标不变。审计请求与单档结果保存该目标。

生成器返回的 `ExpansionDraft` 只有扩写文本 `prompt`：

```json
{
  "prompt": "A cat on a chair in daylight."
}
```

生成目标引导扩写，接受条件仍使用档位范围。例如 Medium 抽到 3，盲校验识别出 2、3 或 4 个新增 PIU 时均满足数量要求。

## blind_expand_validation：独立语义判断

盲校验只接收 `source` 和 `output`。它看不到 level、抽取的 PIU 目标数量、档位范围、其他档位结果或生成历史。

响应 `BlindExpansionValidation` 只有三个字段：

| 字段 | 含义 |
| --- | --- |
| `passed` | 源语义与约束是否完整保留、是否无冲突，以及新增内容是否有意义、相关、具有视觉意义且不冗余 |
| `reason` | 语义判断的原因，用于审计与人工复核 |
| `added_pius` | 从完整输出中独立识别的新增 PIU，每项包含 `fact` 和 `output_evidence` |

每项 PIU 只保留事实和原文证据，不使用 ID、索引或独立计数字段。以 Source `一只猫` 和上面的扩写为例：

```json
{
  "passed": true,
  "reason": "Source preserved; additions are relevant and meaningful.",
  "added_pius": [
    {"fact": "The cat is on a chair", "output_evidence": "on a chair"},
    {"fact": "Daylight", "output_evidence": "in daylight"}
  ]
}
```

即使语义失败也返回识别出的新增 PIU。没有新增信息时，只要忠实保留 Source，语义判断可以通过；是否满足目标数量由程序确定。英文提示词中的原始专名和要求出现在图像中的文字可以保留源语言。

PIU 是文本中可识别的一项视觉事实。新增 PIU 必须引入 Source 既未表达、也未必然隐含的视觉区别。翻译、同义改写、语境重述和泛化质量词不计入新增。例如，“陶艺师拉坯”中的“转盘”译为 pottery wheel，本身不产生新事实。

可独立识别的事实分别计数，即使写在同一短语中；也不能机械拆分修饰词或重复计算重叠事实。校验器需要检查完整输出，避免只列出部分新增内容。原始事实保留、语义矛盾与无意义扩写由整体语义结论及原因表达。

## 程序判定与重新生成

`assess_expansion` 接收盲校验报告、扩写文本和当前数值范围，依次检查：

- 模型的语义 `passed` 必须为 true。
- 文本包含拉丁字母，没有代码围栏，也没有 `light:`、`medium:`、`rich:` 或 `prompt:` 前缀标签。
- 每个 `output_evidence` 都是扩写文本中的连续原文片段，区分大小写。
- `fact` 统一大小写并移除空白、标点和下划线后非空且不重复。
- `len(added_pius)` 落在档位范围内，包括上下界，不要求恰好等于生成目标。

任一检查失败，程序侧 `QualityDecision.passed` 都为 false。决策保存规则失败原因和 `added_count`，数量只取盲校验列表的长度。质量失败时，程序使用原 Source 和该档位最初抽取的目标重新生成，已通过的档位保留结果。

JSON 或 schema 不合法属于技术失败，在同一次逻辑请求内重试。结构合法的报告交给节点判断质量；数量失败记为 `piu_out_of_range:<count>`。报告原因和程序失败原因保存在运行记录中，供审计与人工复核。每档最多生成三次，技术请求每次最多尝试五次。预算耗尽拒绝整组。

## group_check：整组接受条件

程序确认集合恰好包含 Light、Medium、Rich，且各项等级匹配、文本非空、状态为 `passed`、盲校验语义报告与程序决策都已通过。只有满足这些条件，才提交整组并导出三条业务记录和 SFT 数据。

`group_check` 直接依据三档的通过状态决定接受或拒绝整组。各档范围已被程序分别验证，配置要求严格递增且互不重叠。每档独立遵守 Source，可以选择不同的新增细节。

盲校验消除了目标数量和范围对判定的直接影响，但 PIU 识别、语义隐含关系和列表是否完整仍依赖模型。程序只能对返回的列表计算数量，不能证明模型没有漏计或合并事实。人工复核应对照 Source、扩写文本与盲校验 PIU 列表，覆盖误通过、误拒绝和切分分歧。
