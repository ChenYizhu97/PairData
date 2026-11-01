# Configuration and running

[中文](running.md) | **English** · [Home](../README.md)

From the project root, commands default to `recipes/rewrite/recipe.yaml`. Select the complex example in `examples/prompt_pairs/` with `--recipe`. Copy an entire recipe directory to define a new task. `check` validates configuration, samplers, template variables, schemas and rules without calling models. It does not check service credentials or resolve every runtime data path.

```bash
uv run pairdata check
uv run pairdata run --count 20 --seed 42 --out runs/rewrite
```

## Top-level recipe

```yaml
name: my_recipe
models:
  generator:
    temperature: 0.8
  evaluator:
    temperature: 0.0
runtime:
  concurrency: 4
  timeout: 120.0
  max_request_attempts: 5
  retry_initial: 1.0
  retry_max: 30.0
  preview_page_size: 100
```

Both source and target stages are required. Each has its own sampler file, generate/evaluate calls, decision and generation budget. A sampler file containing `{}` is valid. Defaults: 3 generations per stage and 5 requests per logical operation, including initial attempts.

Models support model, base_url, api_key_env, temperature, structured_output (`json` or `native`), reasoning_effort and extra_body. Missing model/base_url use MODEL_NAME / OPENAI_BASE_URL. Model configuration supports `${ENV_NAME}` substitution; api_key_env names the credential variable, defaulting to OPENAI_API_KEY.

Set provider-specific options explicitly in extra_body; the runtime does not override model options based on provider domains.

Process environment overrides `--env-file` (default `.env`); model configuration overrides environment defaults. CLI concurrency, timeout and preview page size override recipe.runtime. Environment files are parsed as text without shell execution or changes to process environment.

## Stage configuration

```yaml
target:
  sampler: samplers/target.yaml
  max_generations: 3
  generate:
    model: generator
    inputs:
      source: {ref: source}
      sample: {ref: sample}
      previous: {ref: previous}
      feedback: {ref: feedback}
    messages:
      - role: user
        template: prompts/target_generate.j2
    response_schema: schemas/target_generate.json
    output_path: text
  evaluate:
    model: evaluator
    inputs:
      source: {ref: source}
      target: {ref: candidate}
      sample: {ref: sample}
    messages:
      - role: user
        template: prompts/target_evaluate.j2
    response_schema: schemas/target_evaluate.json
  decision: rules/target.yaml
```

Source similarly references `samplers/source.yaml`. Define Target fields separately in `samplers/target.yaml`:

```yaml
tone:
  type: categorical
  choices:
    - value: friendly
    - value: formal
```

Generate and evaluate support lists of system/user messages, each referring to a Jinja2 file. The runtime adds the response-schema instruction. Samplers, templates, schemas and rules are relative to recipe.yaml and must remain inside its directory. Template includes, imports and inheritance are unsupported. The random filter is disabled; randomness belongs to the sampler.

Input values are JSON constants or `{ref: field.path}` references, resolved only at the top level of each binding. Paths support object fields and explicit indices, such as generation.items[0].text. output_path also supports `$` for the complete response.

| Call | Available references |
| --- | --- |
| Source generate | sample, previous, feedback |
| Source evaluate | sample, generation, candidate |
| Target generate | source, sample, previous, feedback |
| Target evaluate | source, sample, generation, candidate |

generation is the complete current response; candidate is the extracted value. Both stages start with null previous/feedback. On regeneration, previous contains that stage's prior candidate, and feedback contains its evaluation/decision. Bind these optionally in the recipe and use them in templates. Target uses its own sample and the accepted Source candidate; Source sampling and evaluation records are outside Target's input context.

Templates see only explicitly bound variables. Undefined variables raise errors. Use Jinja2 default or is defined for optional sampled fields, and tojson for JSON values.

## Resume and review

Repeat a run command with `--resume`. New runs require a nonexistent output directory. Resume requires matching requests, models, samplers, templates, schemas and rules. Concurrency and preview page size may change. Credential values are excluded from fingerprints. recipe.json archives configuration with expanded samplers and resource contents; the supplied recipe is reloaded and checked on resume.

Count is the planned task count; rejected tasks are not replaced. Concurrency limits in-flight model requests across the whole run.

```bash
uv run python scripts/review_samples.py runs/rewrite --out runs/rewrite-review.csv
```

Review columns are left empty for humans. preview.html is standalone and offline; search and status filtering apply to the current page.
