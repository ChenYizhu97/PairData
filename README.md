# MidasData

Generate visual descriptions primarily in Chinese, with some Chinese descriptions mixed with English keywords and some English descriptions, then expand them into Light, Medium, and Rich English image prompts for auditable, resumable SFT datasets. `generate` and `expand` run independently. Each English level is generated separately; training records are exported only after the entire group passes validation.

The project name is **MidasData**. The Python distribution, import package, and CLI command are all named `midasdata`.

The current contracts follow [prompts.py](src/midasdata/prompts.py) and the packaged [sampler.json](src/midasdata/sampler.json). Sampling has five independent dimensions: `domain`, `focus`, `specificity`, `expression`, and `language`. Expression controls wording style; language controls Chinese (`zh`), English (`en`), or mixed (`mixed`) text. Each dimension has 0.1 dropout; an omitted language imposes no language restriction. Sampler explanations, model instructions, and SFT task instructions are English.

## Installation

Requires Python 3.11+, uv, and a POSIX environment for file locking and durable directory writes. Dependency versions are pinned in `uv.lock`.

```bash
uv sync --locked
uv run midasdata generate --help
uv run midasdata expand --help
```

Run these commands from the MidasData checkout. The module entry point is also available as `uv run python -m midasdata`. If the checkout was moved or renamed, recreate its local virtual environment and run `uv sync --locked` again; editable installs and command scripts can retain absolute paths to the previous checkout.

## Model configuration

The CLI reads `.env` from the working directory, or the file selected by `--env-file PATH`. A minimal configuration is:

```dotenv
base_url=https://your-service.example/v1
model=your-model
api_key=your-secret
```

The canonical names are `OPENAI_BASE_URL`, `MODEL_NAME`, and `OPENAI_API_KEY`. Their aliases are `base_url` / `BASE_URL`, `model` / `MODEL`, and `api_key` / `API_KEY`. Aliases are normalized separately for the process environment and the file before merging, so process values take precedence even across different aliases. Within either source, canonical names take precedence over aliases. `.env` is parsed as text; shell expressions are never executed. Each configuration load resolves its own settings and credentials without modifying the process environment. API key values are omitted from configuration snapshots and redacted from provider errors.

To configure separate generation and validation models or change the Medium and Rich PIU ranges:

```bash
cp config.example.toml config.toml
```

Precedence is CLI options > TOML > environment-derived model defaults > built-in defaults. `${ENV_NAME}` interpolation applies to TOML strings, including nested dictionaries and lists. `api_key_env` specifies an environment variable name. The `generator` and `validator` sections independently configure `model`, `base_url`, `temperature`, and `structured_output`; both default to the environment's model and endpoint. The CLI defaults to temperature 0.8 for generation and 0.0 for validation.

`structured_output="json"` asks for JSON text and parses it inside the technical retry boundary. `"native"` uses the provider's JSON Schema interface. Both modes enforce the same Pydantic response contracts. Choose a mode supported by the provider: a non-retryable HTTP error, including an unsupported-interface 400 response, stops the run. Changing the mode requires a new output directory. HTTP(S) environment proxies remain enabled.

For DeepSeek endpoints, generation and all validation calls explicitly default to `extra_body.thinking.type="disabled"`; `reasoning_effort` defaults to unset. An explicit `extra_body.thinking.type="enabled"` in a model section overrides this default. Other providers receive only their configured parameters. Model sections accept `reasoning_effort` and `extra_body`; the commented examples in [config.example.toml](config.example.toml) show where these settings belong.

| Setting | Default |
| --- | --- |
| `concurrency` | 4 simultaneous model requests |
| `timeout` | 120 seconds per request |
| `preview_page_size` | 100 completed samples per page |
| `retry_initial` / `retry_max` | 1 / 30 seconds for technical retry backoff |
| Added PIUs | Light: 0; Medium: 2–4; Rich: 5–8 |

Light must remain at zero added PIUs. Configured ranges must not overlap and must increase from Light to Rich.

## Running

With an existing `.env`, no TOML file is required:

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded
```

With TOML:

```bash
uv run midasdata generate --config config.toml --count 100 --seed 42 --out runs/generated
uv run midasdata expand --config config.toml --input runs/generated/data.jsonl --out runs/expanded
```

`--model` overrides only the generation model; `--validator-model` overrides only the validation model. `--base-url` and `--structured-output` apply to both. `--concurrency` limits actual model requests across all samples and levels, and `--timeout` is measured in seconds.

`--count` is the number of planned tasks and must be nonnegative. Rejected tasks are not replaced. Expansion accepts Chinese, English, or mixed UTF-8 JSONL records such as `{"id":"sample-1","prompt":"一只橘猫趴在窗边"}`, meaning one orange cat lying by a window, as well as `generate` output with its `sample` field. Other input fields are rejected. Duplicate input IDs are rejected before any model requests.

## Resuming

A new run requires an output directory that does not already exist. After interruption, use the original arguments with `--resume`:

```bash
uv run midasdata generate --count 100 --seed 42 --out runs/generated --resume
uv run midasdata expand --input runs/generated/data.jsonl --out runs/expanded --resume
```

The fingerprint includes the request, sampler, configuration, software version, prompt versions and file content digest, and SFT template version. For expansion, the request includes both the resolved input path and its content digest. Only `concurrency` and `preview_page_size` are excluded from the configuration fingerprint; other settings, including timeout and retry delays, must match. API key values are not included, so rotating a key without changing its configured variable name does not change the fingerprint. Changed versions or contracts require a new run; old artifacts are not supported.

Committed passed and rejected samples are skipped. Uncommitted samples replay from the graph entry point, reuse saved successful responses, and retain their technical and generation budgets. Requests whose outcomes are unknown still consume technical attempts. Terminal rejections never receive a fresh budget on resume.

Derived exports and previews are rebuilt from committed results. A damaged task file or a newline-terminated invalid audit entry blocks recovery. Recovery discards only the final audit line if it lacks a terminating newline.

## Artifacts and preview

Run directories contain `tasks.jsonl`, `sources.json`, `manifest.json`, `results/`, `data.jsonl`, `lineage.jsonl`, `rejected.jsonl`, `run.lock`, and `preview.html`. `audit.jsonl` is created when the first audit event is written; a zero-task run does not need it. `expand` additionally exports `sft.jsonl`. See [data contracts and provenance](docs/data.md) for their schemas and lifecycle.

Open `preview.html` directly in a browser. Model text is displayed as text, and search and filters work without a network connection or backend. Runs exceeding `preview_page_size` use `preview/` pages; search and filters apply only to the current page. Rejected samples retain available intermediate text and validation evidence. A replacement draft has no current validation until its own validator returns a valid response; earlier evidence remains in the audit log.

Manifest statistics distinguish `planned`, `passed`, `rejected`, and `pending`. They include request counts, technical retries, quality revisions, reported token usage, rejection reasons, and planned/passed sampling distributions. `complete` means that all planned tasks finished and exports and previews were written; rejected samples may still be present.

## Verification and human review

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
```

Tests cover configuration isolation, five-dimensional sampling, language conditions, data contracts, PIU evidence, termination rules, draft state, and recovery. Test inputs and call histories are created from current contracts in temporary directories; tests require no saved runs, historical fixtures, or remote model requests. Model-quality evaluations require actual service runs and human review of both accepted and rejected samples.

The source tree contains one implementation of the current contracts. Default sampling data is packaged with the module, so installed commands do not need files from this checkout. New runs use only their explicit inputs and configuration; `--resume` reads only the selected run directory.

Create a review CSV from committed results:

```bash
uv run python scripts/review_samples.py runs/expanded --out runs/expanded-review.csv
```

The sheet contains source text, available level outputs, automatic status and rejection reason, and blank fields for human assessments. The script does not approve samples automatically.

## Troubleshooting

- Authentication failures, missing models, and other non-retryable HTTP errors stop the run while preserving completed results. Fix the service settings and resume only if the fingerprint still matches.
- Timeouts, connection errors, HTTP 408/409/429 or 5xx responses, and invalid JSON/schema responses allow at most five technical attempts per logical call. Exhaustion rejects the current sample.
- Quality failures allow at most three generations per source description or English level. Single-level and group revisions share each level's budget.
- `insufficient_expansion_space` from a single-level validator or from either scope of the group validator terminates the whole expansion group. Source constraints take precedence over addition targets.
- `run directory is already in use` means another executor still holds the run lock.
- Automatic acceptance rates do not establish dataset quality. PIU segmentation and thresholds need human calibration.

See [data contracts](docs/data.md), [sampling](docs/sampler.md), [workflows and recovery](docs/workflows.md), and [validation](docs/validation.md).
