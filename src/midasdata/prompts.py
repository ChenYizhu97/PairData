"""视觉提示词的生成、盲校验和 SFT 指令。

Generation, blind validation, and SFT instructions for visual prompts.
"""

import json
from textwrap import dedent

SFT_SYSTEM = (
    "Convert the source description into a concise English "
    "image-generation prompt. Preserve all original visual facts "
    "and constraints. Add relevant, non-redundant visual facts "
    "according to the requested level."
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

        Do not expose sampled dimension names, values, or metadata
        as generation instructions in the user prompt.
        Return only the requested concept and prompt fields.
        The user prompt must contain no sampling metadata or explanations.

        On revision, address the feedback while retaining the visual concept
        unless the concept itself conflicts with the sampled conditions.
    """).strip(),
    "blind_sampler_validation": dedent("""
        Validate the source image-generation prompt against its sampled conditions.
        Check only supplied dimensions; omitted dimensions are unrestricted.

        Evaluate condition matching, visual content, and prompt cleanliness.
        Specificity reflects the amount and precision of explicit visual information,
        not text length. All expression styles are acceptable.
        Language must match the sampled language condition, if supplied.

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
        Convert the source description into a concise English
        image-generation prompt.

        Preserve all original visual facts and constraints, including
        quantities, negations, subjects, attributes, actions,
        relationships, styles, and on-image text.

        A PIU is a distinct, independently identifiable visual fact.
        Add exactly target_added_pius new PIUs relative to the source.

        Each addition must introduce a visual distinction not already
        expressed or necessarily implied by the source.
        Translation and paraphrasing do not count as additions.

        Count independently identifiable visual facts separately,
        even when expressed together, without splitting overlapping
        facts or modifiers merely to increase the count.

        Additions must be relevant, novel, and non-redundant.
        Avoid generic quality terms, repetitive details, and filler.
        Use concise natural prose or keyword-style phrasing.

        When target_added_pius is 0, preserve the source without
        introducing new visual facts.
        Explicit source constraints always take priority.

        Return only prompt, without a PIU list, counts, or explanations.
    """).strip(),
    "blind_expand_validation": dedent("""
        Validate the English image-generation prompt against its source.
        Check semantic preservation, contradictions, visual additions,
        and output format.

        A PIU is a distinct, independently identifiable visual fact.

        Check preservation of all original PIUs, including quantities, negations,
        subjects, attributes, actions, relationships, styles,
        and on-image text. Do not return a separate original PIU list.

        Identify every added PIU relative to the source.
        An addition must introduce a visual distinction not already
        expressed or necessarily implied by the source.
        Translation and paraphrasing do not count as additions.

        Count independently identifiable visual facts separately,
        even when expressed together, without splitting overlapping
        facts or modifiers merely to increase the count.

        Report all valid added PIUs, regardless of their number.
        Do not merge independent facts, omit additions, or duplicate
        the same fact. Redundant, uncertain, or meaningless additions
        must fail the semantic check and be explained in reason.

        Each added PIU has only fact and output_evidence, without an ID or index.
        output_evidence must be a continuous, verbatim substring of the
        output text, preserving exact case and punctuation.
        Do not translate, combine, or invent evidence.
        Evidence spans may overlap when supporting distinct PIUs.

        An empty added PIU list is valid when no additions exist.
        English wording is required, except original proper names and
        requested on-image text that may retain their source language.
        Reject unrelated explanations, labels, or code in the output.

        Return only passed, reason, and added_pius.
        Set passed to true only if source meaning and constraints are
        fully preserved, without contradictions, and additions are
        meaningful, relevant, visual, and non-redundant.
        Explain the semantic judgment in reason, with actionable issues
        on failure. Return identified added PIUs even when passed is false.
        Do not judge counts or expansion levels, or rewrite the prompt.
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
