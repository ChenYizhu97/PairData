# ExDataset

ExDataset contains **150 synthetic source descriptions and 450 English image prompts** for training and evaluating controllable prompt translation/expansion. Each Chinese, English, or mixed-language source has **Light / Medium / Rich** outputs, targeting **0 / 2–4 / 5–8** added visual information units (PIUs).

| Split | Sources | Expanded / SFT records |
| --- | ---: | ---: |
| train | 100 | 300 |
| valid | 20 | 60 |
| test | 30 | 90 |

Splits use normalized **source text** as the grouping key, domain stratification, and fixed seed **20261013**. All three levels and every associated audit record stay in the same split. Text and IDs are preserved from the two delivered batches. Exact membership and the deterministic allocation method are recorded in `splits.json` and `metadata.json`.

**Synthesis:** MidasData **0.1.6**, commit **`25d9ef65b00101ef446060d108046c1f2502d3c5`**, with DeepSeek API model **`deepseek-flash`** for both generation and validation (`https://api.deepseek.com`). Temperatures: **0.8 / 0.0**; structured output: **JSON**; thinking: **disabled**; reasoning effort: unset; timeout: **120 s**. Concurrency was **8** for the first 50-group batch and **16** for the next 100-group batch. The default five-dimensional sampler used **0.1 dropout**; generation/technical attempt limits were **3 / 5**. Complete configurations, seeds and instruction snapshots are in `metadata.json`; API credentials are not included.

Each split contains:

- `source-prompts.jsonl`, `expanded-prompts.jsonl`, `sft.jsonl`: source records, level-specific outputs, and three-message training examples.
- `pairs.jsonl`, `provenance.jsonl`: source-to-level alignment and links to the archived records.
- `generation-*` / `expansion-*` JSONL: full results, tasks, lineage and **verbatim audit events**, including validation, PIU evidence, feedback and intermediate failed attempts for that split's sources.
- `run-metadata.json`: snapshots of the original runs' configurations and provenance.

Open **[preview.html](preview.html)** directly for offline browsing, split filters, three-level comparison and expandable validation/audit details. `checksums.sha256` covers all release files.

The data passed MidasData's automatic checks; it is synthetic and not a fully human-annotated benchmark. No prompts were regenerated or rewritten during this release's assembly.
