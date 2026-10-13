"""Generation, validation, and SFT instructions for visual prompts."""

import json
from textwrap import dedent

SFT_SYSTEM = (
    "Translate and expand source image descriptions into English "
    "image-generation prompts according to the specified expansion level, "
    "while strictly preserving the original semantics."
)

SYSTEMS = {
    "generate": dedent("""
        Generate realistic user descriptions for image prompt.
        Choose a specific visual concept matching the sampled conditions,
        and return both the concept and the description.

        Only sampled conditions are constraints; omitted dimensions are unrestricted.
        Specificity controls the amount of explicit visual information, not verbosity.
        Keep descriptions concise and natural, including highly specific ones.
        Do not write exhaustive image captions.

        Follow the requested expression style: keywords, natural language,
        or colloquial requests. language=zh means Chinese, language=en means
        English, and language=mixed means primarily Chinese with some English
        visual terms. Mixed-language English must describe visual content.

        For domain=art_design, explicitly specify a design format such as
        illustration, icon, logo, poster, packaging, or typography.
        A request like "help me draw" alone does not establish a design format.

        Sampling conditions guide generation but must not appear as labels,
        metadata, or instructions in the description.
        Produce valid visual content without explanations, code, or irrelevant chat.
        On revision, address the feedback while preserving the intended concept.
    """).strip(),

    "validate_source": dedent("""
        Validate the source image description against its sampled conditions.
        Check only supplied dimensions; omitted dimensions are unrestricted.

        Evaluate condition matching, visual content, and prompt cleanliness.
        Specificity measures explicitly stated visual information, not raw length.
        Keywords, natural language, colloquial requests, and mixed language
        are all acceptable.

        language=zh requires Chinese, language=en requires English, and
        language=mixed requires primarily Chinese with English visual terms.
        English terms must describe visual content, not sampling parameters.

        For domain=art_design, the source must explicitly name a design format
        such as illustration, icon, logo, poster, packaging, or typography.
        "Help me draw" alone is insufficient.

        Provide satisfied and evidence for each sampled dimension.
        Evidence and visual_evidence must each be a short, continuous,
        verbatim substring of the source, preserving case and punctuation.
        Never translate, combine fragments, or include explanations in evidence.

        Reject non-visual content, gibberish, and unrelated explanations.
        Provide specific, actionable feedback without rewriting the description.
    """).strip(),

    "expand": dedent("""
        Convert the source description into a concise English image-generation
        prompt at the requested expansion level.

        Preserve every explicit visual fact, including quantities, negations,
        subjects, attributes, actions, relationships, styles, and on-image text.

        A PIU must represent a complete visual fact that can be traced to the text.
        Spatial relationships require identifiable participants;
        isolated relation words are not independent PIUs.

        Light adds 0 PIUs. Medium defaults to 3 new PIUs; Rich defaults to 6.
        Follow the supplied PIU range when different. Internally count
        independent additions and aim for the middle of the allowed range.

        Added PIUs must be relevant, novel, non-redundant, and visual.
        Synonyms, adjective stacking, and generic quality terms do not count.
        Express all information concisely without padding or repetition.

        Explicit source constraints take priority over expansion targets.
        Do not violate restrictions such as "only" or "do not add".

        If previous_output and feedback are provided, revise the previous
        version. When over-expanded, remove only added facts; when
        under-expanded, add meaningful visual facts without losing the source.

        Return only a JSON object with a prompt field.
    """).strip(),

    "validate_english": dedent("""
        Validate the English prompt against the source and requested level.
        Check semantic preservation, contradictions, added PIUs, and format.

        A PIU must represent a complete visual fact that can be traced to the text.
        Spatial relationships require identifiable participants;
        isolated relation words are not independent PIUs.
        Identify every original PIU, including
        quantities, negations, subjects, attributes, actions, relationships,
        styles, and on-image text. All must be preserved.

        For each added PIU, verify relevance, novelty, and visual meaning.
        Synonyms, adjective stacking, and generic quality terms do not count.

        Evidence must be a continuous, verbatim substring of the source;
        output_evidence must be a continuous, verbatim substring of the output.
        Preserve exact case and punctuation. Never translate evidence,
        combine fragments, or include explanations in evidence fields.
        Use short representative fragments.

        original_index must start at 0, cover all original PIUs, and contain
        no duplicates. Evaluate actual textual facts, not just verbal claims.

        Light must add exactly 0 PIUs; an empty added list is valid and
        meaningful_addition should be true when no meaningless additions exist.
        Medium and Rich must satisfy their supplied PIU ranges.

        If strict source constraints make the target impossible, mark
        insufficient_expansion_space. Reject explanations, labels, or code
        outside the English image prompt. Provide actionable feedback.
    """).strip(),

    "validate_group": dedent("""
        Validate the Light, Medium, and Rich prompts together against
        the same source description.

        A PIU must represent a complete visual fact that can be traced to the text.
        Spatial relationships require identifiable participants;
        isolated relation words are not independent PIUs.
        Extract original_pius once at the top level,
        using consistent segmentation and 0-based indices.

        For each level, independently verify preservation of all original
        PIUs and identify valid added PIUs. Report preserved, added, and
        issues separately.

        Added PIUs must be relevant, novel, non-redundant, and visual.
        Synonyms, adjective stacking, and generic quality terms do not count.
        Added PIU counts must strictly increase from Light to Medium to Rich,
        with every level satisfying its own supplied range.

        Evidence must be a continuous, verbatim substring of the source;
        output_evidence must be a continuous, verbatim substring of the
        corresponding output. Preserve exact case and punctuation.
        Do not translate or combine evidence fragments.
        original_index must cover all original PIUs without duplicates.

        Levels are independent: added details may differ or conflict
        across levels, provided each remains consistent with the source.
        Higher levels need not include lower-level additions.
        Do not judge expansion by word count.

        Light may have no additions; this is valid, and meaningful_addition
        should be true when no meaningless additions exist.
        Mark insufficient_expansion_space when source constraints prevent
        the required additions.

        If validation fails, retry_levels must contain every problematic
        level; otherwise it must be empty. Provide specific, actionable
        feedback without generating replacement prompts.
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
