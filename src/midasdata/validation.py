"""纯函数：将模型给出的证据转成质量决策，不调用模型、不更新工作流。

Pure functions turn model evidence into quality decisions without model calls or state updates.
"""

import re

from midasdata.config import ExpansionRanges
from midasdata.schemas import (
    EXPANSION_LEVELS,
    ExpansionLevel,
    ExpansionSetValidation,
    ExpansionValidation,
    LevelValidation,
    OriginalPIU,
    QualityDecision,
    SourceValidation,
)


def assess_source(report: SourceValidation, conditions: dict, prompt: str) -> QualityDecision:
    reasons = []
    if not report.visual or report.visual_evidence not in prompt:
        reasons.append("missing_visual_evidence")
    if not report.clean or "```" in prompt:
        reasons.append("invalid_source_format")
    chinese = bool(re.search(r"[\u3400-\u9fff]", prompt))
    latin = bool(re.search(r"[A-Za-z]", prompt))
    language = conditions.get("language")
    if (
        (language == "zh" and not chinese)
        or (language == "en" and (not latin or chinese))
        or (language == "mixed" and (not chinese or not latin))
    ):
        reasons.append("invalid_source_language")
    if set(report.conditions) != set(conditions):
        reasons.append("condition_keys_mismatch")
    for dimension in conditions:
        evidence = report.conditions.get(dimension)
        if evidence is None or not evidence.satisfied:
            reasons.append(f"condition_not_satisfied:{dimension}")
        elif evidence.evidence not in prompt:
            reasons.append(f"condition_evidence_not_found:{dimension}")
    return QualityDecision(passed=not reasons, reasons=reasons)


def distinct_facts(facts: list[str]) -> bool:
    normalized = [re.sub(r"[\W_]+", "", fact.casefold()) for fact in facts]
    return len(set(normalized)) == len(normalized)


def assess_level(
    report: LevelValidation,
    original_pius: list[OriginalPIU],
    source_prompt: str,
    expanded_prompt: str,
    level: ExpansionLevel,
    ranges: ExpansionRanges,
) -> QualityDecision:
    reasons = []
    if not report.semantic_preservation or report.missing_pius:
        reasons.append("semantic_omission")
    if not report.no_contradiction or report.contradictions:
        reasons.append("contradiction")
    # Light 没有新增事实，meaningful_addition=false 可以表示“没有新增”。
    # Light adds no facts, so meaningful_addition=false can mean no additions were made.
    if not report.meaningful_addition and (level != "light" or report.added_pius):
        reasons.append("meaningless_addition")
    if (
        not report.valid_format
        or not re.search(r"[A-Za-z]", expanded_prompt)
        or "```" in expanded_prompt
        or re.match(r"\s*(?:light|medium|rich|prompt)\s*:", expanded_prompt, re.I)
    ):
        reasons.append("invalid_english_format")
    if (
        not original_pius
        or not distinct_facts([item.fact for item in original_pius])
        or any(item.evidence not in source_prompt for item in original_pius)
    ):
        reasons.append("invalid_original_evidence")
    indices = [item.original_index for item in report.preserved_pius]
    if sorted(indices) != list(range(len(original_pius))) or any(
        item.output_evidence not in expanded_prompt for item in report.preserved_pius
    ):
        reasons.append("incomplete_preservation_evidence")
    additions = [
        item
        for item in report.added_pius
        if item.relevant and item.novel and item.visual and item.output_evidence in expanded_prompt
    ]
    if len(additions) != len(report.added_pius) or not distinct_facts(
        [item.fact for item in additions]
    ):
        reasons.append("invalid_added_evidence")
    interval = getattr(ranges, level)
    if not interval.min <= len(additions) <= interval.max:
        reasons.append(f"piu_out_of_range:{len(additions)}:{interval.min}-{interval.max}")
    if report.insufficient_expansion_space:
        reasons.append("insufficient_expansion_space")
    return QualityDecision(
        passed=not reasons,
        reasons=reasons,
        added_count=len(additions),
        insufficient_expansion_space=report.insufficient_expansion_space,
    )


def assess_expansion(
    report: ExpansionValidation,
    source_prompt: str,
    expanded_prompt: str,
    level: ExpansionLevel,
    ranges: ExpansionRanges,
) -> QualityDecision:
    return assess_level(report, report.original_pius, source_prompt, expanded_prompt, level, ranges)


def expansion_set_findings(
    report: ExpansionSetValidation,
    source_prompt: str,
    expanded_prompts: dict[ExpansionLevel, str],
    ranges: ExpansionRanges,
) -> tuple[list[ExpansionLevel], list[str]]:
    decisions = {
        level: assess_level(
            report.levels[level],
            report.original_pius,
            source_prompt,
            expanded_prompts[level],
            level,
            ranges,
        )
        for level in EXPANSION_LEVELS
    }
    problems = [level for level in EXPANSION_LEVELS if not decisions[level].passed]
    reasons = [f"{level}:{reason}" for level in problems for reason in decisions[level].reasons]
    for lower, higher in zip(EXPANSION_LEVELS, EXPANSION_LEVELS[1:]):
        if decisions[lower].added_count >= decisions[higher].added_count:
            if higher not in problems:
                problems.append(higher)
            reasons.append(f"{higher}:piu_not_increasing")
    return problems, reasons


def check_expansion_set_contract(
    report: ExpansionSetValidation,
    source_prompt: str,
    expanded_prompts: dict[ExpansionLevel, str],
    ranges: ExpansionRanges,
) -> None:
    """检查报告是否完整且修订指令自洽；不决定是否接受扩写结果。

    Check report completeness and revision consistency without accepting or rejecting expansions.
    """
    if set(report.levels) != set(EXPANSION_LEVELS):
        raise ValueError("group validation must contain light, medium and rich")
    if len(report.retry_levels) != len(set(report.retry_levels)):
        raise ValueError("retry_levels must not contain duplicates")
    if report.insufficient_expansion_space or any(
        item.insufficient_expansion_space for item in report.levels.values()
    ):
        return
    # 修订列表需覆盖证据暴露的问题；不完整的报告应重新请求校验。
    # Revisions must cover problems found in the evidence; incomplete reports need revalidation.
    problems, _ = expansion_set_findings(report, source_prompt, expanded_prompts, ranges)
    if not set(problems).issubset(report.retry_levels):
        raise ValueError("retry_levels must include every failing level")
    if report.retry_levels and not report.feedback.strip():
        raise ValueError("requested revisions require feedback")


def assess_expansion_set(
    report: ExpansionSetValidation,
    source_prompt: str,
    expanded_prompts: dict[ExpansionLevel, str],
    ranges: ExpansionRanges,
) -> QualityDecision:
    """节点对已通过契约检查的报告计算业务质量结果。

    Nodes assess business quality after the report has passed its contract checks.
    """
    if report.insufficient_expansion_space or any(
        item.insufficient_expansion_space for item in report.levels.values()
    ):
        return QualityDecision(
            passed=False,
            reasons=["insufficient_expansion_space"],
            retry_levels=report.retry_levels,
            insufficient_expansion_space=True,
        )
    problems, reasons = expansion_set_findings(report, source_prompt, expanded_prompts, ranges)
    if report.retry_levels:
        reasons.append("group_revision_requested")
    return QualityDecision(
        passed=not problems and not report.retry_levels,
        reasons=reasons,
        retry_levels=report.retry_levels,
    )
