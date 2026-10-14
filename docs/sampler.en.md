# Five-dimensional sampling

[中文](sampler.md) | **English** · [Home](../README.en.md)

The default sampler is packaged as `src/midasdata/sampler.json` and loaded as a package resource. No sampler file in the working directory is required. Explanations are English model input data and are passed through unchanged.

## Dimensions and defaults

| Dimension | Options and weights |
| --- | --- |
| `domain` | `people` 0.20, `animals` 0.15, `objects` 0.15, `scenes` 0.15, `plants_food` 0.10, `vehicles` 0.10, `art_design` 0.10, `cultural_abstract` 0.05 |
| `focus` | `subject` 0.20, `action` 0.20, `scene` 0.15, `relation` 0.15, `composition` 0.15, `aesthetic` 0.15 |
| `specificity` | `minimal` 0.40, `moderate` 0.60 |
| `expression` | `keywords` 0.35, `natural` 0.40, `colloquial` 0.25 |
| `language` | `zh` 0.75, `en` 0.15, `mixed` 0.10 |

Each dimension independently drops out with probability 0.1. If retained, its option is chosen using that dimension's weights. All dimensions may drop out, producing `{}`. The CLI uses this fixed dropout probability; it has no dropout override.

`expression` controls keywords, natural sentences, or colloquial requests. Source prompts are primarily Chinese: the default language weights are 75% Chinese, 10% Chinese mixed with English visual keywords, and 15% English, conditional on retaining the language dimension. `language` independently controls these choices. `mixed` is a language option, not an expression option. A dropped language dimension imposes no language restriction. The validator still checks visual content and cleanliness.

`generate --seed 42` uses a separate `random.Random`. Tasks are sampled in dimension order (`domain`, `focus`, `specificity`, `expression`, `language`) and written to `tasks.jsonl` before workers start, so response order, technical retries, and quality revisions cannot change the sampling sequence. Quality revisions preserve the task ID and sampled conditions.

Dropped dimensions are not checked as conditions. `specificity` controls explicitly stated visual information, while expansion `level` controls new English visual information; they are independent. `minimal` centers on one main subject or scene with few specified details. `moderate` includes several concrete visual attributes, relationships, or constraints. Both stay concise and natural; verbosity and exhaustive captions are not goals.

A fixed seed reproduces sampling, not model wording or the randomly assigned run ID. Original responses are retained in the audit log. `--count` is the planned task count; rejections do not trigger replacement sampling. The manifest reports planned and passed distributions for all five dimensions, using `(dropout)` for absent dimensions. Expansion records without sampling metadata also appear under `(dropout)` in those statistics.

## Custom samplers

```bash
uv run midasdata generate --count 10 --seed 42 --sampler custom-sampler.json --out runs/sampler-check
```

A custom file must contain exactly the five dimension keys, with a nonempty option list for each. Every row is `[option, explanation, weight]`. For example:

```json
{
  "domain": [["animals", "Animals and creatures", 1.0]],
  "focus": [["subject", "Focus on the subject", 1.0]],
  "specificity": [["minimal", "One main visual idea", 1.0]],
  "expression": [["natural", "Natural-language description", 1.0]],
  "language": [["en", "User descriptions written entirely in English", 1.0]]
}
```

Startup validation requires supported, unique option strings, nonblank explanation strings, and finite nonnegative numeric weights. Boolean weights are rejected, and weights in each dimension must sum to 1 within an absolute tolerance of `1e-9`. Zero-weight options are allowed.

Custom samplers may change weights or select a subset of the existing options; they cannot add dimensions or enum values. `null` is not a valid sampling option. Omission is produced only by dropout and is represented by leaving the dimension out of the sampled dictionary. Changing any sampler content, including explanations, requires a new run because the entire sampler participates in the recovery fingerprint.
