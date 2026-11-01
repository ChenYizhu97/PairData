# Sampling

[中文](sampler.md) | **English** · [Home](../README.md)

Source and Target use separate `samplers/source.yaml` and `samplers/target.yaml` files. Reference them with `sampler: samplers/source.yaml` or `sampler: samplers/target.yaml` in recipe.yaml. Fields are sampled independently, once before the stage's first generation. Target sampling happens only after Source passes.

Sampler files directly declare fields, for example:

```yaml
language:
  type: categorical
  dropout: 0.1
  choices:
    - value: zh
      weight: 3.0
    - value: en
      weight: 1.0
limit:
  type: integer
  min: 10
  max: 30
```

Categorical selects a JSON value by weight, default weight 1. Weights must be finite and nonnegative with a positive sum; the sampler normalizes them. Choices must be distinct. Values may be strings, numbers, booleans, null, arrays or objects.

Integer samples uniformly from an inclusive integer interval. Each field supports dropout from 0 to 1, default 0. Dropped fields are absent from sample. A sampler file containing `{}` produces an empty object. Field names are simple identifiers starting with a letter or underscore. Files are validated at startup and included in snapshots and resume fingerprints.

Seeds derive from the user seed, stable task index, stage and field name, independently of completion order or mapping order. Run IDs are random. A seed reproduces sampling, not model wording.

Quality regeneration, technical retries and resume preserve the original sample. Rejected Sources do not produce Target samples.

Manifest distributions record sampled_tasks, not_reached and sampled / stage_passed / exported field distributions per stage. Values are JSON-encoded counter keys; absent fields use `(dropout)`. Unreached stages are counted separately.

Count is the planned task count. Rejections are not replaced, so filtering may change the final distribution. Statistics report this shift without quota filling or automatic reweighting.

The example in `examples/prompt_pairs/` uses five Source dimensions with 0.1 dropout. Its Target expansion field samples a target/min/max object. The 0, 2–4 and 5–8 intervals have equal total weight, with uniform targets within each interval. These are recipe values, not built-in levels.
