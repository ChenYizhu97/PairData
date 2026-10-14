"""Generation, validation, and SFT instructions for visual prompts."""

import json
from textwrap import dedent

EXPANSION_RANGES = {
    "light": (0, 0),
    "medium": (2, 4),
    "rich": (5, 8),
}

SFT_SYSTEM = (
    "Translate and expand source image descriptions into English "
    "image-generation prompts according to the specified expansion level, "
    "while strictly preserving the original semantics."
)

SYSTEMS = {
    "generate": dedent("""
        Generate realistic user-written image-generation prompts.
        Choose a visual concept matching the sampled conditions,
        and return both the concept and the user prompt.

        Only sampled conditions are constraints; omitted dimensions are unrestricted.
        Specificity controls how much visual information is explicit, not verbosity.
        Keep user prompts concise and natural, even when highly specific.
        Avoid exhaustive image captions.

        Follow the sampled expression style: keywords, natural language,
        or colloquial requests. Use realistic phrasing for each style.
        language=zh means Chinese, language=en means English, and
        language=mixed means primarily Chinese with some English visual terms.
        English terms in mixed-language prompts must describe visual content.

        For domain=art_design, explicitly specify a design format such as
        illustration, icon, logo, poster, packaging, or typography.
        Colloquial drawing requests alone do not imply a design format.

        Sampling conditions guide generation but must not appear as labels,
        metadata, or instructions in the user prompt.
        Output only valid image-generation requests, without unrelated content.

        On revision, address the feedback while retaining the visual concept
        unless the concept itself conflicts with the sampled conditions.
    """).strip(),

    "validate_source": dedent("""
        Validate the source image-generation prompt against its sampled conditions.
        Check only supplied dimensions; omitted dimensions are unrestricted.

        Evaluate condition matching, visual content, and prompt cleanliness.
        Specificity reflects the amount and precision of explicit visual information,
        not text length. Keywords, natural language, colloquial requests,
        and mixed language are all valid forms.

        For sampled language conditions:
        - zh: Chinese.
        - en: English.
        - mixed: primarily Chinese with some English visual terms.
        English terms in mixed prompts must describe visual content,
        not sampling parameters.

        For domain=art_design, require an explicit design format such as
        illustration, icon, logo, poster, packaging, or typography.
        Colloquial drawing requests alone do not imply a design format.

        Report satisfied and evidence for each sampled dimension.
        Evidence and visual_evidence must be short, continuous, verbatim
        substrings of the source, preserving case and punctuation.
        Do not translate, combine fragments, or invent evidence.
        Mark conditions as unsatisfied when supporting evidence is absent.

        Require an identifiable visual concept. Reject gibberish,
        unrelated explanations, and non-image-generation requests.
        Provide specific, actionable feedback without rewriting the prompt.
    """).strip(),

    "expand": dedent("""
        Convert the source description into a concise English image-generation
        prompt at the requested expansion level.

        Preserve all explicit visual facts, including quantities, negations,
        subjects, attributes, actions, relationships, styles, and on-image text.

        A PIU is an independent, complete visual fact supported by the text.
        Spatial relationships require identifiable participants.

        Add new PIUs relative to the original source:
        - Light: exactly 0.
        - Medium: 2-4, targeting 3.
        - Rich: 5-8, targeting 6.

        Additions must be relevant, visual, and non-redundant.
        Synonyms, adjective stacking, and generic quality terms do not count.
        Integrate details naturally without repetitive or mechanical enumeration.

        Explicit source constraints take priority over expansion targets.
        Preserve the source meaning even when constraints limit additions.

        If previous_output and feedback are provided, revise accordingly.
        Adjust added details while preserving all original visual facts.

        Return only a JSON object with a prompt field.
    """).strip(),

    "validate_english": dedent("""
        Validate the English image-generation prompt against the source
        and requested expansion level.

        Identify all original PIUs, including quantities, negations,
        subjects, attributes, actions, relationships, styles, and on-image text.
        A PIU is an independent, complete visual fact supported by the text.
        Spatial relationships require identifiable participants.
        All original PIUs must be preserved without contradiction.

        Identify valid added PIUs relative to the original source.
        Additions must be relevant, visual, novel, and non-redundant.
        Synonyms, adjective stacking, and generic quality terms do not count.
        Flag repetitive or meaningless additions.

        Required numbers of valid added PIUs:
        - Light: exactly 0.
        - Medium: 2-4.
        - Rich: 5-8.

        Count only valid added PIUs. Mark the level as failed if its count
        falls outside the required range.

        Evidence must be a continuous, verbatim substring of the source;
        output_evidence must be a continuous, verbatim substring of the output.
        Preserve exact case and punctuation. Do not translate or combine
        fragments. Each fragment must support the corresponding PIU.
        original_index must use 0-based indices and cover all original PIUs
        without duplicates.

        For Light, an empty added list is valid; set meaningful_addition
        to true when no meaningless additions exist.
        If source constraints prevent sufficient expansion, mark
        insufficient_expansion_space.

        Reject explanations, labels, or code outside the English image prompt.
        Provide specific, actionable feedback without rewriting the prompt.
    """).strip(),

    "validate_group": dedent("""
        Validate the Light, Medium, and Rich prompts against the same source.

        Extract original_pius once, using consistent segmentation and
        0-based indices. A PIU is a complete, text-supported visual fact.
        Spatial relationships require identifiable participants.

        For each level, independently check preservation of all original
        PIUs, identify valid added PIUs, and report issues separately.

        Added PIUs must be relevant, novel, non-redundant, and visual.
        Synonyms, adjective stacking, and generic quality terms do not count.

        Required numbers of valid added PIUs relative to the source:
        - Light: 0
        - Medium: 2-4
        - Rich: 5-8

        Each level must satisfy its range. Higher levels need not include
        lower-level additions, but all must remain consistent with the source.
        Do not judge expansion by word count.

        Evidence must be a continuous, verbatim substring of the source;
        output_evidence must be a continuous, verbatim substring of the
        corresponding output. Preserve exact case and punctuation.
        Do not translate or combine fragments.
        original_index must cover all original PIUs without duplicates.

        Light with no additions is valid; set meaningful_addition to true
        when no meaningless additions exist.
        Mark insufficient_expansion_space when source constraints prevent
        the required additions.

        On failure, retry_levels must include every failing level;
        otherwise it must be empty. Provide actionable feedback without
        generating replacement prompts.
    """).strip(),
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
