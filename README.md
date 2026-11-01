# PairData

**English** | [中文](README.zh-CN.md)

A configurable, LangGraph-based pipeline for synthesizing source–target datasets with distribution-aware sampling, LLM-assisted evaluation, and programmatic quality control.

Each accepted task exports exactly one pair:

```json
{"source": "A cat beside a window", "target": "A cat resting beside a sunlit window."}
```

The default recipe lives in `recipes/rewrite/`; complex examples live in `examples/`. YAML defines models and input bindings, with separate Source and Target sampler files. Jinja2 renders messages; JSON Schema validates responses; declarative rules decide acceptance.

```text
Source Sampler → Source → Evaluate → Gate → Target Sampler → Target → Evaluate → Gate → Export
```

A failed gate retries its own generation stage within budget. Both Source and Target can bind `previous` and `feedback` to the prior candidate, evaluation and rule decision. Both examples enable this. Sampling happens once per stage, and Target retries preserve the accepted Source. Rejected tasks are not replaced.

## Quick start

Requires Python 3.11+, uv, and a POSIX environment with file locking and directory fsync.

```bash
uv sync --locked
uv run pairdata check
```

Configure an OpenAI-compatible service in `.env`:

```dotenv
MODEL_NAME=your-model
OPENAI_BASE_URL=https://your-service.example/v1
OPENAI_API_KEY=your-secret
```

```bash
uv run pairdata run --count 20 --seed 42 --out runs/rewrite
```

The run produces `data.jsonl`, `audit.jsonl`, `manifest.json`, `recipe.json` and an offline `preview.html`. Repeat the command with `--resume` after interruption.

## Default recipe and examples

From the project root, omitting `--recipe` selects `recipes/rewrite/recipe.yaml`. Copy a complete recipe directory to configure your own task.

| Recipe | Task |
| --- | --- |
| [rewrite](recipes/rewrite/recipe.yaml), default | Generate a short message, then rewrite it in a sampled tone |
| [prompt_pairs](examples/prompt_pairs/recipe.yaml), complex example | Generate a visual description and an English prompt with sampled additions |

```bash
uv run pairdata check --recipe examples/prompt_pairs/recipe.yaml
uv run pairdata run --recipe examples/prompt_pairs/recipe.yaml --count 20 --out runs/prompt-pairs
```

`prompt_pairs` samples one expansion target per task and exports one pair. Its Target evaluator sees only Source and Target; code checks the identified PIU list length. Semantic judgments and individual PIU evidence remain model assessments.

## Documentation

- [Configuration and running](docs/running.en.md)
- [Sampling](docs/sampler.en.md)
- [Evaluation and decisions](docs/validation.en.md)
- [Workflow](docs/workflows.en.md)
- [Data contracts](docs/data.en.md)
- [Architecture design, Chinese](docs/architecture.md)

## Checks

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check .
```

Tests use temporary directories and mocked model transports, without remote calls.
