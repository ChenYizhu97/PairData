# MidasData

[中文](README.md) | **English**

MidasData generates primarily Chinese visual source descriptions, then independently expands each description into Light, Medium, and Rich English image prompts. Training data is exported only when the entire group passes validation. Generation and expansion can run separately, model requests and quality decisions are auditable, and interrupted runs can resume.

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

You can also copy `config.example.toml` and use `--config config.toml` to configure separate generator and validator models and PIU ranges. Configuration precedence is CLI > TOML > environment defaults > built-in defaults.

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

Sampling and prompts are defined in `sampler.py`, `sampler.json`, and `prompts.py`. Sampling uses five independent dimensions: domain, focus, specificity, expression, and language. Tasks are saved before concurrent requests begin. Rejected tasks are not replaced; `--count` is the planned task count. Expansion accepts `{ "id": "sample-1", "prompt": "一只橘猫趴在窗边" }` and also accepts the `sample` field from generation output. Unknown fields and duplicate IDs are rejected before model requests.

## Reading the code

Start with the three workflows. Each file follows **state → nodes → routes → build**:

| File | Responsibility |
| --- | --- |
| [source_generation.py](src/midasdata/workflows/source_generation.py) | Generate, validate, revise, and finalize a source description |
| [expansion_attempt.py](src/midasdata/workflows/expansion_attempt.py) | Generate and validate once at a specified expansion level |
| [prompt_expansion.py](src/midasdata/workflows/prompt_expansion.py) | Run three levels in parallel, share budgets, target revisions, and validate the group |

Expansion levels consistently use `level` (Light, Medium, Rich), with the type `ExpansionLevel`. `expansion_attempt` means one generation and validation attempt at a specified level.

The main call chain is **runner → workflow nodes → execution.execute → LLMModel**:

| Module | Responsibility |
| --- | --- |
| `runner.py` | Create models and shared execution resources, schedule samples, commit results, and export |
| `workflows/` | Prepare prompts, execute operations, compute quality decisions from evidence, update state, and route |
| `execution.py` | Replay history, limit concurrency, perform technical retries, and record ordinary async operations |
| `model.py` | Adapt model interfaces through `LLMModel`, send messages, and parse responses |
| `storage.py` | Save run configuration, tasks, operation history, quality decisions, and results through `RunStore` |

`ExecutionContext` holds only shared storage, a concurrency semaphore, and backoff parameters. `LLMModel` holds no run storage or workflow state. Nodes compute quality decisions using pure functions in `validation.py`, then routes handle the results; the execution layer returns only parsed operation outputs. Ordinary state computations and branch selection happen directly in the workflows.

Each remaining module has one responsibility: `schemas.py` defines response and storage contracts, `planning.py` plans tasks, `exports.py` exports results, and `preview.py` builds offline previews. The CLI handles arguments and result reporting.

Nodes do not hold per-sample object state or export files directly. The single-attempt expansion subgraph has no quality revision loop: the parent graph manages all revisions, with individual and group revisions sharing a budget of three generations per level. The execution layer uses an explicit technical retry loop. Model operations allow at most five request attempts, including the first; callers specify retry rules for other operations, which execute once by default.

See [Workflows and recovery](docs/workflows.en.md) for the full flow, and [Data contracts](docs/data.en.md), [Sampling](docs/sampler.en.md), and [Validation](docs/validation.en.md) for data and quality rules.

## Recovery and outputs

A new run requires an output directory that does not yet exist. After an interruption, use the original arguments with `--resume`:

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated --resume
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded --resume
```

Recovery skips committed passed and rejected samples, replays uncommitted tasks using saved model responses, and preserves technical and generation budgets. Concurrency and preview page size may change; other configuration, inputs, the sampler, and prompts must match. API key values are excluded from fingerprints and configuration snapshots.

The run directory contains `tasks.jsonl`, `sources.json`, `manifest.json`, `results/`, `audit.jsonl`, `data.jsonl`, `lineage.jsonl`, `rejected.jsonl`, `run.lock`, and `preview.html`; expansion also exports `sft.jsonl`. A run with zero tasks does not need an audit file. Derived outputs can be rebuilt from committed results, with atomic replacement per file.

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
