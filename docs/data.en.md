# Data contracts and audit

[中文](data.md) | **English** · [Home](../README.en.md)

Each data.jsonl row contains exactly source and target:

```json
{"source": "The meeting starts at 10.", "target": "Please join the meeting at 10."}
```

Values come from each generation's output_path and are constrained by recipe schemas. They may be text or structured JSON. Business rows contain no IDs, samples, levels or messages. Only tasks with two accepted stages export a row, in planned task order. No automatic deduplication, replacement or dataset splitting is performed.

## Run artifacts

| File | Contents |
| --- | --- |
| tasks.jsonl | Stable IDs and task indices |
| results/<sha256(id)>.json | Atomic terminal task results |
| data.jsonl | Accepted source-target pairs |
| lineage.jsonl | Task ID, status and one-based data.jsonl row number |
| rejected.jsonl | Rejected tasks and current stage details |
| audit.jsonl | Sampling, requests, raw responses, parsing successes, decisions, errors |
| recipe.json | Configuration with expanded samplers and referenced resource text |
| manifest.json | Configuration, fingerprint, status, usage, distributions |
| run.lock | Operating-system directory lock |
| preview.html, preview/ | Offline comparison and evaluation details |

TaskResult contains id, status, source, target and reason. Each StageResult retains sample, generation_attempt, generation, candidate, evaluation, decision, status and reason. Target is null until reached. Task statuses are passed/rejected; stage statuses are pending/passed/quality_failed/technical_failed.

Recipe schemas validate dynamic responses; Pydantic validates fixed internal records. Accepted tasks require both stage statuses and decisions to pass.

## Persistence

OperationEvent contains run_id, sample_id, stage, logical_call_id, generation_attempt, technical_attempt, model, timestamp, event and data. Stage names include source.sample, target.generate, target.evaluate and target.gate.

Request payloads retain explicit inputs, actual messages and response schemas. Raw responses are saved before validation. Source and Target Generate can bind previous/feedback. Target can also bind the accepted source and retains its own sample across regeneration.

Decisions contain passed, reasons and checks. Checks record paths, operators, actual/expected values, outcomes and optional errors. Sampling and decisions do not consume model tokens. Usage sums integer fields actually reported by model responses.

Audit appends flush/fsync. File replacement uses temporary files, fsync, atomic rename and directory fsync. Atomicity is per file; derived exports rebuild on resume.

Credential values come from environment variables and are excluded from snapshots and fingerprints. Audit retains business content for traceability.
