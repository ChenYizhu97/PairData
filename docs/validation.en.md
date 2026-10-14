# Validation rules

[中文](validation.md) | **English** · [Home](../README.en.md)

Models judge semantics and identify added facts; code enforces deterministically computable rules. An expansion passes only when its blind semantic judgment and code checks both pass. `group_check` decides group acceptance. See the [project overview](../README.en.md) for the business flow.

## blind_sampler_validation: source description checks

The source validator (`blind_sampler_validation` / `SourceValidation`) evaluates only sampled dimensions and returns a condition result and evidence for each. The program requires exactly those condition keys, `satisfied=true`, and evidence that appears as a contiguous, verbatim substring of the description. It also requires `visual=true`, locatable `visual_evidence`, and `clean=true`. Descriptions must not contain a Markdown code fence. For `language=zh` or `mixed`, a Chinese character is required; `mixed` also requires a Latin letter. For `language=en`, a Latin letter is required and Chinese characters are disallowed. Dropped language imposes no character-language restriction. Predominance, actual English wording, and whether mixed terms describe visual content remain model judgments.

Prompts allow keywords, natural language, and colloquial image requests according to `expression`; Chinese, English, and mixed Chinese/English are independently controlled by `language`. For example, `帮我画一只狗` asks for a dog image, while `养狗好吗` asks whether owning a dog is a good idea. The validator is instructed to reject unrelated conversation, garbled content, explanations, and copied sampling labels. Mixed-language words must describe visual content.

For `art_design`, the validator is instructed to require an explicit design form such as illustration, icon, logo, poster, graphic design, packaging, or typography. A drawing request and subject alone are insufficient. Its evidence must quote the specified form. This is a model-evaluated semantic rule; the program checks the returned condition result and evidence location, without a separate design-keyword classifier.

Unsatisfied conditions and missing evidence are recorded separately as `condition_not_satisfied:<dimension>` and `condition_evidence_not_found:<dimension>`.

## Expand: generation toward a sampled target count

Python uniformly samples an integer from each level's inclusive range. Every generator request contains only `source` and the fixed `target_added_pius`.

| Internal level | Default added PIU range |
| --- | --- |
| Light | 0 |
| Medium | 2–4 |
| Rich | 5–8 |

Light is fixed at zero; Medium and Rich are configurable, with strictly increasing, nonoverlapping ranges. Source facts and constraints take precedence over addition targets. Counts must not be met by changing quantities, negation, relationships, styles, or requested in-image text.

The random seed derives from the run ID, task ID, and level. Each level is sampled once during workflow initialization; regeneration, technical retries, and resume all preserve that target. Request audits and per-level results save the target.

The generator's `ExpansionDraft` contains only the expanded text in `prompt`:

```json
{
  "prompt": "A cat on a chair in daylight."
}
```

The generation target guides expansion; acceptance still uses the level's range. For example, when Medium draws 3, blind counts of 2, 3, or 4 all satisfy the count requirement.

## blind_expand_validation: independent semantic assessment

Blind validation receives only `source` and `output`. It sees no level, sampled target PIU count, level range, other levels, or generation history.

`BlindExpansionValidation` has exactly three fields:

| Field | Meaning |
| --- | --- |
| `passed` | Whether source meaning and constraints are fully preserved without contradictions, and additions are meaningful, relevant, visual, and non-redundant |
| `reason` | Explanation of the semantic judgment for auditing and human review |
| `added_pius` | Additions independently identified throughout the output, each containing `fact` and `output_evidence` |

Each PIU contains only its fact and verbatim evidence, without an ID, index, or separate count field. For Source `一只猫` and the expansion above:

```json
{
  "passed": true,
  "reason": "Source preserved; additions are relevant and meaningful.",
  "added_pius": [
    {"fact": "The cat is on a chair", "output_evidence": "on a chair"},
    {"fact": "Daylight", "output_evidence": "in daylight"}
  ]
}
```

The model returns identified additions even when semantics fail. An output with no additions may pass semantically if it faithfully preserves the Source; code determines whether the count meets its target. Original proper names and requested in-image text may retain their source language.

A PIU is a distinct visual fact identifiable in the text. An added PIU introduces a distinction that the Source neither expresses nor necessarily implies. Translation, paraphrasing, contextual restatement, and generic quality terms do not count. For example, translating the wheel as a pottery wheel in the context of a potter throwing clay adds no new fact by itself.

Independently identifiable facts are counted separately even when expressed in one phrase, without mechanically splitting modifiers or double-counting overlapping facts. The validator must inspect the entire output to avoid listing only some additions. Preservation failures, contradictions, and meaningless expansions are expressed through the overall semantic judgment and its reason.

## Code decisions and regeneration

`assess_expansion` receives the blind report, expanded text, and current numeric range and checks:

- The model's semantic `passed` must be true.
- Text contains a Latin letter, has no code fence, and has no leading `light:`, `medium:`, `rich:`, or `prompt:` label.
- Each `output_evidence` is a continuous verbatim substring of the expansion, matching case.
- Facts are nonempty and distinct after case folding and removal of whitespace, punctuation, and underscores.
- `len(added_pius)` falls within the level's range, including both endpoints, without requiring an exact match to the generation target.

Any failed check makes the code-side `QualityDecision.passed` false. The decision saves rule failure reasons and `added_count`, using only the blind list length for counting. On a quality failure, code regenerates from the original Source and that level's initially sampled target. Passed levels retain their results.

Invalid JSON or schema responses are technical failures retried within the same logical operation. Structurally valid reports proceed to node-level quality assessment; count failures use `piu_out_of_range:<count>`. Report reasons and code failure reasons stay in run records for auditing and human review. Each level allows at most three generations; each logical model operation allows at most five request attempts. Exhausted budgets reject the group.

## group_check: group acceptance conditions

Code requires exactly Light, Medium, and Rich, with matching level identifiers, nonblank prompts, `passed` statuses, and passing blind semantic reports and code decisions. Only then is the group committed and its three business and SFT records exported.

`group_check` directly accepts or rejects the group based on the three levels' passing statuses. Code has already checked each range, and configuration requires strictly increasing, nonoverlapping intervals. Each level independently respects the Source and may choose different additions.

Blind validation removes direct exposure to target counts and ranges, but PIU identification, implied meaning, and list completeness still depend on the model. Code can count the returned list without proving that no facts were omitted or merged. Human review should compare the Source, expanded text, and blind PIU list and cover false acceptances, false rejections, and segmentation disagreements.
