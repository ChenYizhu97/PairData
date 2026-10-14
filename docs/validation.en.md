# Validation rules

[中文](validation.md) | **English** · [Home](../README.en.md)

This contract constrains the outputs of Generate Source and Expand and defines the assessment criteria for Validate Source, Validate English, and Validate Group. See the [project overview](../README.en.md) for their responsibilities and flows.

Automatic validation produces auditable judgments. The model still identifies and segments PIUs: program checks enforce structure, evidence, and counts, but cannot establish that every source fact was found. Human review must cover both accepted and rejected samples.

## Validate Source: source description checks

The source validator (`validate_source` / `SourceValidation`) evaluates only sampled dimensions and returns a condition result and evidence for each. The program requires exactly those condition keys, `satisfied=true`, and evidence that appears as a contiguous, verbatim substring of the description. It also requires `visual=true`, locatable `visual_evidence`, and `clean=true`. Descriptions must not contain a Markdown code fence. For `language=zh` or `mixed`, a Chinese character is required; `mixed` also requires a Latin letter. For `language=en`, a Latin letter is required and Chinese characters are disallowed. Dropped language imposes no character-language restriction. Predominance, actual English wording, and whether mixed terms describe visual content remain model judgments.

Prompts allow keywords, natural language, and colloquial image requests according to `expression`; Chinese, English, and mixed Chinese/English are independently controlled by `language`. For example, `帮我画一只狗` asks for a dog image, while `养狗好吗` asks whether owning a dog is a good idea. The validator is instructed to reject unrelated conversation, garbled content, explanations, and copied sampling labels. Mixed-language words must describe visual content.

For `art_design`, the validator is instructed to require an explicit design form such as illustration, icon, logo, poster, graphic design, packaging, or typography. A drawing request and subject alone are insufficient. Its evidence must quote the specified form. This is a model-evaluated semantic rule; the program checks the returned condition result and evidence location, without a separate design-keyword classifier.

Unsatisfied conditions and missing evidence are recorded separately as `condition_not_satisfied:<dimension>` and `condition_evidence_not_found:<dimension>`.

## Expand: PIUs and expansion requirements

A PIU is a distinct visual fact identifiable in the text that differentiates the depicted content. Count facts rather than words. For example, one orange cat lying by a window specifies the subject, quantity, color, posture, and location. Explicit quantities, negation, relationships, styles, and requested in-image text are source constraints to preserve.

Expand, Validate English, and Validate Group use the same rule for additions: an added PIU must introduce a visual distinction that the source neither expresses nor necessarily implies. Translation, paraphrasing, and contextual restatement do not count; redundant, uncertain, or generic-quality additions are also excluded. For example, in the context of a potter throwing clay, translating the wheel as a pottery wheel does not itself add a visual fact.

Independently identifiable visual facts are counted separately even when expressed in one phrase, without mechanically splitting modifiers or double-counting overlapping facts. Validators return PIUs, evidence, indices, and judgment fields. The expansion generator returns an `ExpansionDraft` containing only `prompt`; extra fields, including PIU lists, counts, evidence, and validation results, violate that response contract. Expansion text should use concise, natural prose or keyword-style phrasing without repetition or filler.

Model instructions and the SFT template are defined in `prompts.py`. The file's content digest participates in the recovery fingerprint.

English validation covers semantic preservation, contradictions, meaningful additions, and prompt format. Original proper names and requested in-image text may remain in their source language. Explanations, level labels, and code blocks are forbidden. The program combines the validator's `valid_format` judgment with checks for a Latin letter, no code fence, and no leading `light:`, `medium:`, `rich:`, or `prompt:` label.

| Level | Default added PIUs |
| --- | --- |
| Light | 0 |
| Medium | 2–4 |
| Rich | 5–8 |

The ranges are initial calibration targets. Medium and Rich are configurable in TOML; Light is fixed at zero. Ranges must be strictly ordered and nonoverlapping. Source constraints always take precedence over addition targets.

## Validate English: evidence and quality decisions

Each `ExpansionValidation` contains `original_pius` (`fact`, source `evidence`), `preserved_pius` (zero-based `original_index`, `output_evidence`), `missing_pius`, `contradictions`, `added_pius` (`fact`, `output_evidence`, `relevant`, `novel`, `visual`), the four quality judgments, `insufficient_expansion_space`, and `feedback`.

The program requires:

- Semantic preservation and no-contradiction judgments to pass, with empty missing-fact and contradiction lists.
- A nonempty original PIU list, distinct fact descriptions, and source evidence that can be located verbatim.
- Preservation indices covering every original PIU exactly once, without gaps, duplicates, or out-of-range values; every output evidence string must appear in the English prompt.
- Distinct added facts, locatable output evidence, and `relevant`, `novel`, and `visual` all true for every addition.
- The number of valid additions to fall within the current level's interval.

Fact distinctness is checked within each PIU list after case folding and removal of whitespace and punctuation, including underscores: `orange fur`, `orange-fur`, and `ORANGE_FUR` count as the same fact. Evidence location is case-sensitive. Fact/evidence correspondence and novelty relative to the source still depend on the model's judgments. An unsupported claim of success cannot override failed evidence checks.

A Light prompt with an empty added-PIU list may pass even if the model sets `meaningful_addition=false` to mean that nothing was added. All other semantic, format, evidence, and zero-addition checks still apply.

Human calibration must check that returned lists cover all actual additions and do not miscount facts already expressed or necessarily implied by the source as new. Counts alone cannot reveal omitted or incorrectly segmented PIUs. For example, wooden tools and clay buckets nearby describe two independently identifiable object facts; sharing a sentence does not justify counting them as one. Modifiers and overlapping descriptions should not be mechanically split.

## Validate Group: group checks and termination

The group validator rechecks Light, Medium, and Rich in one response using a common PIU segmentation. `original_pius` appears only at the top level. Each level has preservation indices, additions, missing facts, contradictions, judgments, and feedback, and refers directly to that shared list; nested `original_pius` fields are forbidden. Single-level `ExpansionValidation` still carries its own `original_pius`. Each level is checked against the source and its configured interval, and valid addition counts must increase strictly.

Levels may add different details, even details that conflict with another level, if each independently respects the source. If the source specifies no time of day, dusk in Medium and noon in Rich may both be valid. Levels need not contain earlier levels' sentences.

`check_expansion_set_contract` requires exactly the three supported levels and unique `retry_levels`. Each level’s preservation evidence is checked against the top-level original PIU list. Every problem level must appear in `retry_levels`, with nonblank top-level feedback whenever revisions are requested. Extra requested revision levels are allowed and consume their generation budgets. The prompt asks for actionable feedback and revisions limited to levels that need changes.

The validation node prepares messages and invokes `execution.execute`, which returns the parsed report. The node then explicitly calls the pure assessment function to compute a `QualityDecision` and records it through `RunStore.record_decision`. `LLMModel` handles model transport and parsing; execution handles history, technical retries and recording. Neither computes or returns a business quality decision. The group report parser checks completeness and revision consistency through `check_expansion_set_contract`, keeping malformed reports within the technical retry boundary. Ordinary quality failures are assessed after execution and lead to generation revisions. Replay reuses the saved response, recomputes the decision in the node and avoids duplicate decision events.

A single-level `insufficient_expansion_space` flag, a top-level group flag, or any flag inside the group's levels terminates the entire expansion group. Routing uses the saved decision flag rather than parsing reason strings. A terminal group response must still provide all schema fields, the shared original PIU list, exactly three levels, and unique retry levels, but its retry list and feedback may be empty; repairable-response requirements do not apply.

Human calibration should cover every sampling option, dropped dimensions, quantities, negation, relationships, styles, and strict constraints. Record false acceptances, false rejections, PIU segmentation disagreements, and the reasons for adjustments. Automatic pass rates do not replace those assessments.
