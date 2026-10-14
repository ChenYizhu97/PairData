"""纯函数：将模型给出的证据转成质量决策，不调用模型、不更新工作流。

Pure functions turn model evidence into quality decisions without model calls or state updates.
"""

import re

from midasdata.config import AdditionRange
from midasdata.schemas import (
    EXPANSION_LEVELS,
    BlindExpansionValidation,
    ExpansionLevel,
    ExpansionResult,
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
    return all(normalized) and len(set(normalized)) == len(normalized)


def assess_expansion(
    report: BlindExpansionValidation,
    expanded_prompt: str,
    interval: AdditionRange,
) -> QualityDecision:
    """语义结论与程序规则都通过时才接受，数量以盲校验列表为准。

    Accept only when semantics and code checks pass; count the blind validator's list.
    """
    reasons = []
    if not report.passed:
        reasons.append(f"semantic_failure:{report.reason}")
    if (
        not re.search(r"[A-Za-z]", expanded_prompt)
        or "```" in expanded_prompt
        or re.match(r"\s*(?:light|medium|rich|prompt)\s*:", expanded_prompt, re.I)
    ):
        reasons.append("invalid_english_format")
    if any(item.output_evidence not in expanded_prompt for item in report.added_pius):
        reasons.append("added_evidence_not_found")
    if not distinct_facts([item.fact for item in report.added_pius]):
        reasons.append("duplicate_or_empty_added_fact")
    count = len(report.added_pius)
    if not interval.min <= count <= interval.max:
        reasons.append(f"piu_out_of_range:{count}")
    return QualityDecision(passed=not reasons, reasons=reasons, added_count=count)


def assess_group(expansions: dict[ExpansionLevel, ExpansionResult]) -> QualityDecision:
    """整组只检查固定三档是否齐全且各自已通过，不调用模型。

    Check that all three expected levels exist and have passed, without model calls.
    """
    reasons = []
    if set(expansions) != set(EXPANSION_LEVELS):
        reasons.append("incomplete_expansion_group")
    for level in EXPANSION_LEVELS:
        result = expansions.get(level)
        if result is None:
            continue
        if (
            result.level != level
            or not result.prompt.strip()
            or result.status != "passed"
            or result.validation is None
            or not result.validation.passed
            or result.decision is None
            or not result.decision.passed
        ):
            reasons.append(f"{level}:expansion_not_passed")
    return QualityDecision(passed=not reasons, reasons=reasons)
