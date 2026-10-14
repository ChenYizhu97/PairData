# Running the project

[中文](running.md) | **English** · [Home](../README.en.md)

This page collects environment requirements, configuration, run commands, and checks. See the [project overview](../README.en.md) for goals and business flows, and [workflow design](workflows.en.md) for state, routing, and recovery mechanisms.

## Installation and usage

Requires Python 3.11+, uv, and a POSIX environment with file locking and directory fsync support.

```bash
uv sync --locked
uv run midasdata generate --help
uv run midasdata expand --help
```

Configure the model in a `.env` file in the working directory:

```dotenv
OPENAI_BASE_URL=https://your-service.example/v1
MODEL_NAME=your-model
OPENAI_API_KEY=your-secret
```

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded
```

You can also copy [config.example.toml](../config.example.toml) and use `--config config.toml` to configure separate generator and validator models and PIU ranges. Configuration precedence is CLI > TOML > environment defaults > built-in defaults.

The environment variables are `OPENAI_BASE_URL`, `MODEL_NAME`, and `OPENAI_API_KEY`. Process environment variables take precedence over `.env`. The `.env` file is parsed as text; loading configuration does not modify the process environment. Use `--env-file` to select another file. TOML supports `${ENV_NAME}` interpolation in nested values.

`--model` overrides only the generator, and `--validator-model` overrides only the validator; `--base-url` and `--structured-output` override both roles. Model configuration supports `temperature`, `reasoning_effort`, `extra_body`, and `api_key_env`. Thinking is disabled by default for both DeepSeek roles; explicit settings take precedence.

| Setting | Default |
| --- | --- |
| `concurrency` | 4 model requests in flight |
| `timeout` | 120 seconds per request |
| `preview_page_size` | 100 completed samples per page |
| `retry_initial` / `retry_max` | 1 / 30 seconds of backoff |
| Added PIU ranges | Light 0; Medium 2–4; Rich 5–8 |

`structured_output="json"` parses the model's JSON text, while `"native"` uses the service's JSON Schema interface. Both modes enforce the same response contracts. Nonretryable HTTP errors, such as an unsupported service interface, stop the run.

`--count` is the planned task count; rejected tasks are not replaced. See [data contracts](data.en.md) for expansion input formats and field constraints. Use `--sampler` to select a custom sampler, whose structure is described in [sampling design](sampler.en.md):

```bash
uv run midasdata generate --count 10 --seed 42 --sampler custom-sampler.json --out runs/sampler-check
```

## Recovery and outputs

A new run requires an output directory that does not yet exist. After an interruption, use the original arguments with `--resume`:

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated --resume
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded --resume
```

Recovery skips committed passed and rejected samples, replays uncommitted tasks using saved model responses, and preserves technical and generation budgets. Concurrency and preview page size may change; other configuration, inputs, the sampler, and prompts must match. API key values are excluded from fingerprints and configuration snapshots.

See [run artifacts](data.en.md#run-artifacts) for the files and their purposes. Derived outputs can be rebuilt from committed results, with atomic replacement per file.

Open `preview.html` directly in a browser to inspect source text, the three expansion levels, failure reasons, and evidence. The preview works offline and splits large runs into pages; search and filters apply to the current page. Model content is rendered as text.

```bash
uv run python scripts/review_samples.py runs/expanded --out runs/expanded-review.csv
```

The review CSV leaves human judgment fields blank and does not automatically approve samples. Automatic pass rates do not replace model quality evaluation and human review.

## Essential tests

Offline tests cover key flows, shared budgets, parallel branch convergence, evidence attribution, recovery of model and ordinary operations, technical retries, configuration isolation, input boundaries, and exports. They do not test fixed prompt wording, call remote models, or read project datasets.

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
```
