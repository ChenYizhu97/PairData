# Evaluation and decisions

[中文](validation.md) | **English** · [Home](../README.en.md)

Models generate content or evaluation reports. Code validates response structure and evaluates acceptance rules. Evaluation schemas need not include a fixed passed field.

## JSON Schema

Responses use object-rooted JSON Schema Draft 2020-12. Extracted Source/Target values may be any JSON value. Local `#/...` references are supported; external schema loading is disabled. Extra format validation is not enabled.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Evaluation",
  "type": "object",
  "properties": {
    "acceptable": {"type": "boolean"},
    "score": {"type": "number", "minimum": 0, "maximum": 1},
    "reason": {"type": "string"}
  },
  "required": ["acceptable", "score", "reason"],
  "additionalProperties": false
}
```

Raw responses are saved before JSON parsing, finite-number checks and schema validation. Invalid JSON, schema mismatches and missing output paths consume the current logical request's technical retry budget. Invalid schema definitions stop startup. Native structured responses and JSON text both undergo local validation.

## Rules

```yaml
all:
  - field: evaluation.acceptable
    op: eq
    value: true
  - field: evaluation.score
    op: gte
    value: 0.8
  - field: candidate
    op: length_between
    min: 1
    max: 2000
```

| Operator | Behavior |
| --- | --- |
| all / any / not | Compose rules; all/any require nonempty lists |
| eq / ne | Typed equality; integers and floats are compatible, booleans are not numeric |
| gt / gte / lt / lte | Numeric ordering |
| exists | Check field existence, including null; omit value |
| in | Check membership in the value array |
| contains | String substring or array membership |
| length_between | Inclusive character or array-element count bounds |

Leaves use field/op; comparisons and content checks require value, while length_between requires min/max. Values may be JSON constants or references such as `{ref: sample.threshold}`. Length bounds are nonnegative integers or references. No Python or arbitrary expressions are executed.

Gate context contains sample, generation, candidate and evaluation. Target Gate also has source. Gate visibility does not widen Target Generate inputs.

Missing fields (except exists) and type errors fail the entire decision, even under not or an otherwise successful any. No implicit string-to-number conversions occur.

Decisions retain passed, reasons and individual checks with locations, operators, actual/expected values and errors. Valid responses that fail rules trigger stage-local regeneration. Both stages can bind previous/feedback to use the prior candidate, evaluation and decision. Samples remain fixed; Target also preserves the accepted source. Technical retries reuse the current logical request's inputs.

## Example quality boundaries

rewrite combines the model's acceptable flag with programmatic character length. prompt_pairs combines model semantics, validity and identified PIUs with boolean, issue-list, PIU-count and code-fence checks. Source evidence is checked as an exact substring.

Individual PIU evidence and independent-fact identification remain model assessments. Schema uniqueItems rejects only identical PIU objects, not semantic duplicates. The generic interpreter has no PIU-specific language logic.
