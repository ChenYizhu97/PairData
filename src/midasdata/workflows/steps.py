"""Node behavior and budget decisions; graph topology stays small and explicit."""

from typing import Literal

from langgraph.graph import END

from midasdata.limits import MAX_GENERATION_ATTEMPTS
from midasdata.llm import ModelCaller, TechnicalFailure
from midasdata.sampler import explain
from midasdata.schemas import (
    LEVELS,
    CallContext,
    Decision,
    EnglishDraft,
    EnglishValidation,
    ExpandDetails,
    ExpandedRecord,
    ExpandState,
    ExpandUpdate,
    GenerateDetails,
    GeneratedRecord,
    GenerateState,
    GroupValidation,
    Level,
    LevelResult,
    LevelState,
    SampleResult,
    SourceDraft,
    SourceValidation,
)
from midasdata.validation import english_decision, group_decision, source_decision


def call_context(
    state: GenerateState | ExpandState | LevelState, *, generation_attempt: int | None = None
) -> CallContext:
    context: CallContext = {
        "sample_id": state["id"],
        "round": state.get("round", 0),
        "generation_attempt": (
            state.get("generation_attempt", 0) if generation_attempt is None else generation_attempt
        ),
    }
    if "level" in state:
        context["level"] = state["level"]
    return context


def revision_feedback(*parts: str) -> str:
    return "\n".join(parts)


def technical_reason(error: TechnicalFailure) -> str:
    return f"technical_failure:{error}"


class GenerateSteps:
    def __init__(self, caller: ModelCaller, sampler: dict):
        self.caller, self.sampler = caller, sampler

    async def initialize(self, state: GenerateState) -> GenerateState:
        # The executor already sampled in stable task order before concurrency.
        return {"generation_attempt": 0, "concept": "", "feedback": "", "reason": ""}

    async def generate(self, state: GenerateState) -> GenerateState:
        attempt = state["generation_attempt"] + 1
        context = call_context(state, generation_attempt=attempt)
        payload = {
            "sample": state["sample"],
            "conditions": explain(state["sample"], self.sampler),
            "concept": state["concept"],
            "previous_prompt": state.get("prompt", ""),
            "feedback": state["feedback"],
        }

        try:
            draft = await self.caller.call(
                "generate",
                payload,
                SourceDraft,
                **context,
            )
            return {
                "concept": draft.concept,
                "prompt": draft.prompt,
                "generation_attempt": attempt,
                "validation": None,
                "decision": None,
                "feedback": "",
                "reason": "",
            }
        except TechnicalFailure as exc:
            return {"generation_attempt": attempt, "reason": technical_reason(exc)}

    async def after_generate(self, state: GenerateState) -> str:
        return "reject" if state.get("reason") else "llm_validator1"

    async def assemble_result(self, state: GenerateState) -> GenerateState:
        return {"result": self._build_result(state, True)}

    async def reject(self, state: GenerateState) -> GenerateState:
        return {"result": self._build_result(state, False)}

    async def validate(self, state: GenerateState) -> GenerateState:
        context = call_context(state)
        try:
            validation = await self.caller.call(
                "validate_source",
                {
                    "sample": state["sample"],
                    "conditions": explain(state["sample"], self.sampler),
                    "prompt": state["prompt"],
                },
                SourceValidation,
                **context,
            )
        except TechnicalFailure as exc:
            return {"reason": technical_reason(exc)}
        decision = source_decision(validation, state["sample"], state["prompt"])
        self.caller.record_decision(
            "validate_source",
            decision,
            **context,
        )
        return {
            "validation": validation,
            "decision": decision,
            "feedback": revision_feedback(validation.feedback, "; ".join(decision.reasons)),
        }

    async def route(self, state: GenerateState) -> str:
        if state.get("reason"):
            return "reject"
        decision = state["decision"]
        assert decision is not None, "routing requires a decision for the current draft"
        if decision.passed:
            return "assemble_result"
        return (
            "llm_generator" if state["generation_attempt"] < MAX_GENERATION_ATTEMPTS else "reject"
        )

    def _build_result(self, state: GenerateState, passed: bool) -> SampleResult:
        source = {"id": state["id"], "sample": state["sample"]}
        records = [GeneratedRecord(**source, prompt=state["prompt"])] if passed else []
        return SampleResult(
            id=state["id"],
            workflow="generate",
            status="passed" if passed else "rejected",
            source=source,
            records=records,
            concept=state["concept"],
            reason="" if passed else state.get("reason") or "quality_budget_exhausted",
            details=GenerateDetails(
                prompt=state.get("prompt", ""),
                validation=state.get("validation"),
                decision=state.get("decision"),
                generation_attempt=state["generation_attempt"],
                feedback=state.get("feedback", ""),
            ),
        )


class ExpandSteps:
    def __init__(self, caller: ModelCaller):
        self.caller = caller

    async def initialize(self, state: ExpandState) -> ExpandUpdate:
        return {"round": 0, "pending": list(LEVELS), "levels": {}, "feedback": {}, "reason": ""}

    async def dispatch(self, state: ExpandState) -> ExpandUpdate:
        return {
            "round": state["round"] + 1,
            "group_validation": None,
            "group_decision": None,
        }

    async def assemble_result(self, state: ExpandState) -> ExpandUpdate:
        return {"result": self._build_result(state, True)}

    async def reject(self, state: ExpandState) -> ExpandUpdate:
        return {"result": self._build_result(state, False)}

    async def after_level_generate(self, state: LevelState) -> str:
        return END if state.get("result") else "llm_validator2"

    def level_task(self, state: ExpandState, level: Level) -> LevelState:
        previous = state["levels"].get(level)
        return {
            "id": state["id"],
            "prompt": state["prompt"],
            "level": level,
            "round": state["round"],
            "generation_attempt": (previous.generation_attempt if previous else 0) + 1,
            "feedback": state["feedback"].get(level, ""),
            "previous_output": previous.prompt if previous else "",
        }

    async def generate_level(self, state: LevelState) -> LevelState:
        interval = getattr(self.caller.config.piu, state["level"]).model_dump()
        try:
            draft = await self.caller.call(
                "expand",
                {
                    "source": state["prompt"],
                    "level": state["level"],
                    "piu_range": interval,
                    "feedback": state["feedback"],
                    "previous_output": state.get("previous_output", ""),
                },
                EnglishDraft,
                **call_context(state),
            )
            return {"output": draft.prompt, "result": None}
        except TechnicalFailure as exc:
            return self.failed_level(state, exc)

    def failed_level(self, state: LevelState, error: TechnicalFailure) -> LevelState:
        return {
            "result": self.level_result(state, "technical_failed", reason=technical_reason(error))
        }

    def level_result(
        self,
        state: LevelState,
        status: Literal["passed", "quality_failed", "technical_failed"],
        *,
        validation: EnglishValidation | None = None,
        decision: Decision | None = None,
        reason: str = "",
    ) -> LevelResult:
        return LevelResult(
            level=state["level"],
            round=state["round"],
            generation_attempt=state["generation_attempt"],
            status=status,
            prompt=state.get("output", ""),
            validation=validation,
            decision=decision,
            reason=reason,
        )

    async def validate_level(self, state: LevelState) -> LevelState:
        context = call_context(state)
        try:
            validation = await self.caller.call(
                "validate_english",
                {
                    "source": state["prompt"],
                    "output": state["output"],
                    "level": state["level"],
                    "piu_range": getattr(self.caller.config.piu, state["level"]).model_dump(),
                },
                EnglishValidation,
                **context,
            )
        except TechnicalFailure as exc:
            return self.failed_level(state, exc)
        decision = english_decision(
            validation, state["prompt"], state["output"], state["level"], self.caller.config.piu
        )
        self.caller.record_decision(
            "validate_english",
            decision,
            **context,
        )
        reason = (
            "insufficient_expansion_space"
            if decision.insufficient_expansion_space
            else "; ".join(decision.reasons)
        )
        return {
            "result": self.level_result(
                state,
                "passed" if decision.passed else "quality_failed",
                validation=validation,
                decision=decision,
                reason=reason,
            )
        }

    async def collect(self, state: ExpandState) -> ExpandUpdate:
        current = [state["levels"][level] for level in state["pending"]]
        if any(result.round != state["round"] for result in current):
            raise RuntimeError("collect ran before all tasks in this round completed")
        for result in current:
            if result.status == "technical_failed":
                return {"reason": result.reason}
            if result.decision and result.decision.insufficient_expansion_space:
                return {"reason": "insufficient_expansion_space"}
        failed = [result.level for result in current if result.status != "passed"]
        if any(
            state["levels"][level].generation_attempt >= MAX_GENERATION_ATTEMPTS for level in failed
        ):
            return {"reason": "quality_budget_exhausted"}
        feedback = dict(state["feedback"])
        for level in failed:
            result = state["levels"][level]
            feedback[level] = revision_feedback(result.validation.feedback, result.reason)
        return {"pending": failed, "feedback": feedback}

    async def after_collect(self, state: ExpandState) -> str:
        if state.get("reason"):
            return "reject"
        return "dispatch" if state["pending"] else "level_consistency_check"

    async def validate_group(self, state: ExpandState) -> ExpandUpdate:
        context = call_context(state)
        outputs = {level: state["levels"][level].prompt for level in LEVELS}

        decision: Decision

        def assess_response(value: GroupValidation) -> None:
            # All parsing paths run this callback. Keep its decision for audit
            # and routing rather than assessing the same response again.
            nonlocal decision
            decision = group_decision(value, state["prompt"], outputs, self.caller.config.piu)

        try:
            validation = await self.caller.call(
                "validate_group",
                {
                    "source": state["prompt"],
                    "outputs": outputs,
                    "piu_ranges": self.caller.config.piu.model_dump(),
                },
                GroupValidation,
                **context,
                check=assess_response,
            )
        except TechnicalFailure as exc:
            return {"reason": technical_reason(exc)}
        self.caller.record_decision("validate_group", decision, **context)
        update: ExpandUpdate = {
            "group_validation": validation,
            "group_decision": decision,
            "pending": decision.retry_levels,
        }
        if decision.insufficient_expansion_space:
            return {**update, "reason": "insufficient_expansion_space"}
        if any(
            state["levels"][level].generation_attempt >= MAX_GENERATION_ATTEMPTS
            for level in decision.retry_levels
        ):
            return {**update, "reason": "group_quality_budget_exhausted"}
        feedback = dict(state["feedback"])
        for level in decision.retry_levels:
            feedback[level] = revision_feedback(
                validation.feedback, validation.levels[level].feedback, "; ".join(decision.reasons)
            )
        return {**update, "feedback": feedback}

    async def after_group(self, state: ExpandState) -> str:
        if state.get("reason"):
            return "reject"
        decision = state["group_decision"]
        assert decision is not None, "routing requires a decision for the current group"
        if decision.insufficient_expansion_space:
            return "reject"
        return "assemble_result" if decision.passed else "dispatch"

    def _build_result(self, state: ExpandState, passed: bool) -> SampleResult:
        records = (
            [
                ExpandedRecord(id=state["id"], level=level, prompt=state["levels"][level].prompt)
                for level in LEVELS
            ]
            if passed
            else []
        )
        return SampleResult(
            id=state["id"],
            workflow="expand",
            status="passed" if passed else "rejected",
            source={"id": state["id"], "prompt": state["prompt"]},
            records=records,
            reason="" if passed else state["reason"],
            details=ExpandDetails(
                round=state["round"],
                levels=state["levels"],
                group_validation=state.get("group_validation"),
                group_decision=state.get("group_decision"),
                feedback=state["feedback"],
            ),
        )
