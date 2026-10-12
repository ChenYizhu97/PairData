"""Five versioned prompts share the same response schemas in both API modes."""

import json

VERSIONS = {
    "generate": "source-generate-v5",
    "validate_source": "source-validate-v5",
    "expand": "en-expand-v7",
    "validate_english": "en-validate-v5",
    "validate_group": "group-validate-v6",
}

SFT_VERSION = "sft-v2"
SFT_SYSTEM = "Translate and expand source image descriptions into English image-generation prompts according to the specified expansion level, while strictly preserving the original semantics."

PIU_DEFINITION = "A PIU is a valid visual information element that can independently alter the image and be traced back to the text."

PIU_PRESERVATION_RULE = "Quantities, negations, subjects, attributes, actions, relationships, styles, and on-screen text in the original text must all be identified and preserved; do not omit any explicit facts."

PIU_ADDITION_RULE = "Added PIUs must be relevant, non-redundant, and visual; synonymous expressions, generic quality words (masterpiece, best quality), and adjective stacking do not count."

PIU_VALIDATION_RULES = (
    "Each evidence / output_evidence must be a continuous, verbatim substring of the corresponding original text; do not translate the evidence. "
    "original_index starts from 0, must cover all original PIUs, and must not contain duplicates. "
    "Evaluate relevant, novel, and visual step-by-step. Evidence must correspond to the facts; do not just return verbal judgments."
)

ART_DESIGN_RULE = (
    " When domain=art_design, the prompt must explicitly specify a design format, such as illustration, icon, logo, poster, graphic design, packaging design, or typography. "
    "The design subject can be a person or animal, but simply saying 'help me draw' alongside a subject description does not qualify as art_design; do not automatically treat an image with an unspecified format as an illustration. "
    "When validating this domain, the evidence must contain the design format explicitly written in the original text; if it is not written, satisfied=false, and you must point out the missing design format in the feedback."
)

SOURCE_FORMAT_RULE = (
    " Sampling parameters are only used to control the description and must not be output verbatim into the prompt: do not add process instructions or condition labels like 'Chinese-English mixed description:' or 'moderate details'. "
    "English in a 'mixed' language setting must be used to describe visual objects, attributes, relationships, styles, or composition; restating sampling parameters like 'focus on scene' is not a valid visual description. "
    "Keep legitimate request-style expressions like 'help me draw'; do not misjudge the request style itself as prompt pollution."
)

SOURCE_EVIDENCE_RULE = (
    " evidence and visual_evidence can only copy a single continuous verbatim substring from the description (preserving original punctuation and case). You cannot splice multiple fragments, and you cannot add explanations, outer quotes, category names, or ellipses. "
    "Choose the single most representative short fragment for each item; place all analysis and explanations strictly in the feedback."
)

EXPANSION_REVISION_RULE = (
    " Before generating, internally count the added independent visual facts. Only control the number of facts outside the original text, aiming for the middle of the target range without exceeding the upper limit. "
    "Medium defaults to a target of 3, and Rich defaults to 6 independently added PIUs (if the provided range differs, strictly follow the provided range). Details already in the original text no longer count as additions; do not use a single long sentence to smuggle in many extra facts. "
    "If previous_output is provided, revise based on the previous version and feedback; if there are too many additions, only delete facts outside the original text; if additions are insufficient, preserve original meaning and add specific facts, do not revert to pure translation."
)

LIGHT_VALIDATION_RULE = (
    " It is acceptable for Light's added PIU list to be empty; when there are no additions, meaningful_addition should be true, indicating no meaningless additions; do not reject Light just because it didn't expand. "
    "English articles themselves do not count as added visual facts. All evidence must verbatim copy a continuous fragment from the original text, and English case and punctuation must also match exactly."
)


SYSTEMS = {
    "generate": (
        "You are generating a visual description for an image prompt dataset. Choose a specific visual concept within the given condition boundaries, and return both the concept and the prompt. "
        "If a condition is omitted, do not restrict that dimension. revisions should only improve condition matching and description quality. "
        "specificity is the level of detail in the text. Keywords, natural language, and colloquial requests are allowed (controlled by the 'expression' dimension). language=zh requests Chinese, language=en requests English, and language=mixed requests primarily Chinese with English visual terms. An omitted language imposes no language restriction. Colloquial expressions can include phrases like 'help me draw...'. "
        "The description must have valid visual imagery, no explanatory answers, no code, and no irrelevant chat. Make diverse choices; do not always generate cats or fixed scenes. "
        "If the sampled domain is art_design, even if specificity is minimal, you must explicitly point out a design format (e.g., poster, icon)."
        + ART_DESIGN_RULE
        + SOURCE_FORMAT_RULE
    ),
    "validate_source": (
        "Check if the source image description meets all actual sampling conditions. conditions only contains sampled dimensions; omitted dimensions do not need checking. "
        "Provide satisfied and verbatim evidence from the source description for each dimension. Judge visual and clean; visual_evidence takes verbatim visual evidence from the description. "
        "language=zh requests Chinese, language=en requests English, and language=mixed requests primarily Chinese with English visual terms. An omitted language imposes no language restriction. "
        "Keywords, natural language, colloquial requests, and Chinese-English mixing can all be legitimate; do not misjudge colloquial requests as chat pollution. Reject lack of visual content, gibberish, or extra explanations. "
        "Feedback should be specific and actionable; do not rewrite the answer for the generator."
        + ART_DESIGN_RULE
        + SOURCE_FORMAT_RULE
        + SOURCE_EVIDENCE_RULE
    ),
    "expand": (
        "Generate an English image prompt for the given source image description at the current level, processing only one level at a time. The source may be Chinese, English, or mixed; translate or normalize it into English. Always base it directly on the original text, preserving subjects, quantities, attributes, actions, relationships, styles, negations, and specified text. "
        "Light is strictly faithful translation and normalization, adding 0 PIUs; Medium and Rich add relevant specific visual details according to the given PIU range without violating the original text. "
        "Strict original text constraints take precedence; do not violate limits like 'only' or 'do not add' just to meet expansion quotas. Other levels are not your constraints. Feedback is used for revision; output no labels, explanations, or code blocks."
        f"{PIU_DEFINITION} {PIU_PRESERVATION_RULE} {PIU_ADDITION_RULE} "
        + EXPANSION_REVISION_RULE
        + " Only return a JSON object containing a prompt field; PIU is only used for internal planning, do not output PIU lists, counts, evidence, or validation fields."
    ),
    "validate_english": (
        "Check the English prompt for semantic preservation, lack of contradiction, valid additions, and output format."
        f"{PIU_DEFINITION} {PIU_PRESERVATION_RULE} {PIU_ADDITION_RULE} "
        + PIU_VALIDATION_RULES
        + " Compare against the current level's PIU range. Light must be strictly and faithfully translated, and the number of added PIUs must be exactly 0. Mark insufficient_expansion_space when strict limits in the original text make the target range impossible to achieve. "
        "English allows on-screen text and proper nouns specified in the original text; no extra explanations, level labels, or code blocks are allowed."
        + LIGHT_VALIDATION_RULE
    ),
    "validate_group": (
        "Re-examine the Light, Medium, and Rich versions of the same source text using unified PIU segmentation. The source may be Chinese, English, or mixed. "
        "Extract original_pius uniformly once at the top level. When validating each level, refer directly to the top-level original_pius to assess preservation. "
        "List preserved, added, and issues for each level separately; the number of valid added PIUs must strictly increase across the levels and fit each level's respective range. "
        "The added details of the three levels can be different or even conflict with each other, as long as they respectively match the original text; a higher level is not required to include the exact additions of the previous level, nor is it judged by raw word count."
        f"{PIU_DEFINITION} {PIU_ADDITION_RULE}"
        + PIU_VALIDATION_RULES
        + " retry_levels specifies only the levels that actually require rewriting. If validation fails, it must be non-empty and cover all problematic levels; if it passes, it must be empty. "
        "Feedback must be specific and executable, do not write answers for the generator; mark insufficient_expansion_space when there is no feasible room for expansion."
        + LIGHT_VALIDATION_RULE
    ),
}


def messages(stage: str, payload: dict, schema: type) -> list[dict]:
    return [
        {
            "role": "system",
            "content": SYSTEMS[stage]
            + "\nOnly return a JSON object conforming to the following JSON Schema, without Markdown formatting:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def sft_record(source: str, level: str, output: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SFT_SYSTEM},
            {"role": "user", "content": f"Expansion level: {level}\nSource description: {source}"},
            {"role": "assistant", "content": output},
        ]
    }
