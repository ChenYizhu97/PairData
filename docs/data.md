# Data contracts and provenance

JSONL files use UTF-8, with one JSON object per nonblank line. Business record models reject unknown fields, empty or whitespace-only IDs and prompts, and unsupported levels. IDs and prompts are stripped of leading and trailing whitespace.

## Business records

Source-description generation exports:

```json
{"id":"gen:run001:000001","sample":{"domain":"animals"},"prompt":"一只橘猫趴在窗边"}
```

The example describes one orange cat lying by a window. `sample` supports `domain`, `focus`, `specificity`, `expression`, and `language`; language is `zh | en | mixed`, and expression is `keywords | natural | colloquial`. Source prompts are primarily Chinese, with some Chinese mixed with English keywords and some English. Dropped dimensions are omitted from `sample`; `{}` is valid. The chosen visual concept is retained in internal results, audit responses, and lineage, but is not part of the business record. Generated IDs contain the run ID and a stable task sequence number.

The expansion graph receives only:

```json
{"id":"gen:run001:000001","prompt":"一只橘猫趴在窗边"}
```

The CLI accepts either this `SourceRecord` shape or a `GeneratedRecord` with `sample`. It validates the input, saves its full accepted shape in the task list, and projects it to `id` and `prompt` before invoking the graph. Bare source records have no sampling metadata; the executor still records the resolved input path as their origin. Input IDs must be unique after whitespace normalization. Arbitrary extra input metadata is not accepted.

Expansion exports:

```json
{"id":"gen:run001:000001","level":"light","prompt":"An orange cat lying by a window."}
```

`level` is `light | medium | rich`, and `(id, level)` identifies an expanded record. Three rows are exported only if the entire group passes. Rows within a group are ordered Light, Medium, Rich; groups follow input order.

`sft.jsonl` uses the `sft-v2` template exactly as defined in `prompts.py`. Each row contains a `messages` array with three roles: system describes the expansion task, user supplies `Expansion level: <level>\nSource description: <source>`, and assistant contains the accepted English prompt. The system and user task instructions are English. The template uses the language-neutral label `Source description:`; source text is preserved as received. IDs and levels are not separate fields in the SFT row; the level is in the user message. Business and SFT exports are derived from the same committed results. There is no automatic train/validation split, deduplication, or merging.

## Run artifacts

| Path | Content |
| --- | --- |
| `tasks.jsonl` | Stable, validated task sequence saved before model requests |
| `results/<sha256(id)>.json` | Complete terminal `SampleResult`, atomically committed per sample; expansion includes the entire group |
| `data.jsonl` | Business records for passed samples only |
| `sft.jsonl` | Training messages for passed expansion groups only |
| `sources.json` | Source type, request/input digest, sampler, and software version |
| `lineage.jsonl` | Original task metadata, internal concept, and terminal status for every committed sample |
| `audit.jsonl` | Request attempts, raw responses and usage, parsed successes, program decisions, and errors; created on the first event |
| `rejected.jsonl` | Full rejected sample results, including reasons and available intermediate details |
| `manifest.json` | Configuration without API key values, request and task digests, versions, run status, and export statistics |
| `run.lock` | Executor lock file; the OS lock is released when the store closes, and the file remains |
| `preview.html`, `preview/page-*.html` | Standalone browser preview and optional pages of completed samples |

Audit events distinguish `technical_attempt` (at most five within one logical call) from `generation_attempt` (at most three per source sample or English level). `logical_call_id` is derived from sample ID, stage, level, round, and generation attempt. Each request saves an input payload digest to detect inconsistent replay. Token statistics aggregate integer usage fields returned by the service; absent usage remains empty, and costs are not estimated.

The executor performs storage writes synchronously in its event loop. Each audit append is flushed and fsynced; the request event is durable before the model request starts. Task files, manifests, source metadata, committed results, exports, and previews use temporary files, fsync, atomic replacement, and directory fsync. Replacement is atomic per file, not across all exported files; resume rebuilds exports after an interruption. `run.lock` prevents two executors from writing the same run concurrently.

## Internal result structure

`SampleResult.workflow` selects the record and detail types:

| Workflow | `records` | `details` |
| --- | --- | --- |
| `generate` | `GeneratedRecord` list | `GenerateDetails`: draft `prompt`, current `validation` and `decision`, `generation_attempt`, and `feedback` |
| `expand` | `ExpandedRecord` list | `ExpandDetails`: `round`, `levels`, current `group_validation` and `group_decision`, and per-level `feedback` |

Each entry in `ExpandDetails.levels` is a `LevelResult` with level, round, generation attempt, status, prompt, optional validation and decision, and reason. Its status is `passed`, `quality_failed`, or `technical_failed`. These are single-level statuses; the enclosing sample status is `passed` or `rejected`.

Records and details from the wrong workflow and unknown nested fields are rejected. `source` allows extensible provenance metadata. Storage, export, and the review script read typed models; preview generation serializes them to embedded JSON.

Optional detail fields are fully serialized with their defaults, including `null` for absent validation and decisions. Dropped dimensions are omitted only from business records' `sample`. The decision's `insufficient_expansion_space` flag is saved together with the original validation and rejection reason. Artifacts are read and written using the current contracts in `schemas.py`.

When a new draft replaces old text, its current validation and decision are cleared until that draft is validated. Previous evidence remains in `audit.jsonl`. Entering a new expansion round also clears the previous group validation and decision, while carrying revision feedback to the requested levels.
