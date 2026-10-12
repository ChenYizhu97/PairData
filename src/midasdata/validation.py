"""Model judgments are necessary, but never sufficient to accept a sample."""

import re

from midasdata.config import PIUConfig
from midasdata.schemas import (
    LEVELS,
    Decision,
    EnglishValidation,
    GroupValidation,
    Level,
    SourceValidation,
)


def source_decision(result: SourceValidation, sample: dict, prompt: str) -> Decision:
    reasons = []
    if not result.visual or result.visual_evidence not in prompt:
        reasons.append("missing_visual_evidence")
    if not result.clean or "```" in prompt:
        reasons.append("invalid_source_format")
    has_chinese = bool(re.search(r"[\u3400-\u9fff]", prompt))
    has_latin = bool(re.search(r"[A-Za-z]", prompt))
    language = sample.get("language")
    if (
        (language == "zh" and not has_chinese)
        or (language == "en" and (not has_latin or has_chinese))
        or (language == "mixed" and (not has_chinese or not has_latin))
    ):
        reasons.append("invalid_source_language")
    if set(result.conditions) != set(sample):
        reasons.append("condition_keys_mismatch")
    for dimension in sample:
        check = result.conditions.get(dimension)
        if check is None or not check.satisfied:
            reasons.append(f"condition_not_satisfied:{dimension}")
        elif check.evidence not in prompt:
            reasons.append(f"condition_evidence_not_found:{dimension}")
    return Decision(passed=not reasons, reasons=reasons)


def _distinct(facts: list[str]) -> bool:
    normalized = [re.sub(r"\W+", "", fact.casefold()) for fact in facts]
    return len(set(normalized)) == len(normalized)


def english_decision(
    result: EnglishValidation, source: str, prompt: str, level: Level, piu: PIUConfig
) -> Decision:
    reasons = []
    if not result.semantic_preservation or result.missing_pius:
        reasons.append("semantic_omission")
    if not result.no_contradiction or result.contradictions:
        reasons.append("contradiction")
    # A faithful Light translation has no additions to assess. Providers may
    # use false to mean "none added"; its evidence and zero-PIU rule still apply.
    if not result.meaningful_addition and (level != "light" or result.added_pius):
        reasons.append("meaningless_addition")
    if (
        not result.valid_format
        or not re.search(r"[A-Za-z]", prompt)
        or "```" in prompt
        or re.match(r"\s*(?:light|medium|rich|prompt)\s*:", prompt, re.I)
    ):
        reasons.append("invalid_english_format")
    originals = result.original_pius
    if (
        not originals
        or not _distinct([item.fact for item in originals])
        or any(item.evidence not in source for item in originals)
    ):
        reasons.append("invalid_original_evidence")
    indices = [item.original_index for item in result.preserved_pius]
    if sorted(indices) != list(range(len(originals))) or any(
        item.output_evidence not in prompt for item in result.preserved_pius
    ):
        reasons.append("incomplete_preservation_evidence")
    valid_additions = [
        item
        for item in result.added_pius
        if item.relevant and item.novel and item.visual and item.output_evidence in prompt
    ]
    if len(valid_additions) != len(result.added_pius) or not _distinct(
        [item.fact for item in valid_additions]
    ):
        reasons.append("invalid_added_evidence")
    added = len(valid_additions)
    interval = getattr(piu, level)
    if not interval.min <= added <= interval.max:
        reasons.append(f"piu_out_of_range:{added}:{interval.min}-{interval.max}")
    if result.insufficient_expansion_space:
        reasons.append("insufficient_expansion_space")
    return Decision(
        passed=not reasons,
        reasons=reasons,
        added_count=added,
        insufficient_expansion_space=result.insufficient_expansion_space,
    )


def check_group_structure(result: GroupValidation) -> None:
    """Verify the level and revision structure before assessing quality."""
    if set(result.levels) != set(LEVELS) or len(set(result.retry_levels)) != len(
        result.retry_levels
    ):
        raise ValueError("group validation requires exactly three unique levels")


def check_group_revision(result: GroupValidation, problems: list[Level]) -> None:
    """A repairable response must identify every affected level and give feedback."""
    if problems and not set(problems).issubset(result.retry_levels):
        raise ValueError("failed group response must identify all affected retry_levels")
    if result.retry_levels and not result.feedback.strip():
        raise ValueError("group revision requires actionable feedback")


def group_decision(
    result: GroupValidation, source: str, prompts: dict[Level, str], piu: PIUConfig
) -> Decision:
    """Assess once; contract errors remain inside the model's parsing boundary."""
    check_group_structure(result)
    if result.insufficient_expansion_space or any(
        item.insufficient_expansion_space for item in result.levels.values()
    ):
        return Decision(
            passed=False,
            reasons=["insufficient_expansion_space"],
            retry_levels=result.retry_levels,
            insufficient_expansion_space=True,
        )
    decisions = {
        level: english_decision(
            EnglishValidation(
                **result.levels[level].model_dump(), original_pius=result.original_pius
            ),
            source,
            prompts[level],
            level,
            piu,
        )
        for level in LEVELS
    }
    problems = [level for level in LEVELS if not decisions[level].passed]
    counts = [decisions[level].added_count for level in LEVELS]
    if counts[0] >= counts[1] and "medium" not in problems:
        problems.append("medium")
    if counts[1] >= counts[2] and "rich" not in problems:
        problems.append("rich")
    check_group_revision(result, problems)
    reasons = [f"{level}:{reason}" for level in problems for reason in decisions[level].reasons]
    if result.retry_levels:
        reasons.append("group_revision_requested")
    return Decision(
        passed=not problems and not result.retry_levels,
        reasons=reasons,
        retry_levels=result.retry_levels,
    )
