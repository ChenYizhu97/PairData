"""Replacement drafts cannot inherit validation evidence from previous text."""

import unittest

from midasdata.llm import TechnicalFailure
from midasdata.sampler import load_sampler
from midasdata.schemas import GroupValidation, LevelResult
from midasdata.validation import group_decision
from midasdata.workflows.expand import build_level
from midasdata.workflows.generate import build_generate
from midasdata.workflows.steps import ExpandSteps
from tests.support import OUTPUTS, SOURCE, ScriptedCaller, expansion_case, source_validation


class DraftStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_source_replacement_clears_evidence_when_validation_fails(self):
        for failure_stage in ("generate", "validate_source"):
            with self.subTest(failure_stage=failure_stage):
                caller = ScriptedCaller()
                first = {"concept": "猫", "prompt": SOURCE}
                second = {"concept": "橘猫", "prompt": "一只橘猫"}
                invalid = source_validation()
                invalid["clean"], invalid["feedback"] = False, "Remove explanations."
                failure = TechnicalFailure("budget exhausted")
                caller.responses[("generate", None)] = [
                    first,
                    failure if failure_stage == "generate" else second,
                ]
                caller.responses[("validate_source", None)] = [invalid, failure]
                result = (
                    await build_generate(caller, load_sampler()).ainvoke(
                        {"id": failure_stage, "sample": {}}
                    )
                )["result"]
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.details.generation_attempt, 2)
                if failure_stage == "validate_source":
                    self.assertEqual(result.details.prompt, second["prompt"])
                    self.assertEqual(result.concept, second["concept"])
                    self.assertIsNone(result.details.validation)
                    self.assertIsNone(result.details.decision)
                    self.assertEqual(result.details.feedback, "")
                else:
                    self.assertEqual(result.details.prompt, first["prompt"])
                    self.assertEqual(result.details.validation.model_dump(), invalid)
                    self.assertFalse(result.details.decision.passed)

    async def test_dispatch_clears_group_evidence_before_failed_validation(self):
        caller = ScriptedCaller()
        steps = ExpandSteps(caller)
        validation = GroupValidation.model_validate(expansion_case()["validation"])
        state = {
            "id": "group",
            "prompt": SOURCE,
            "round": 1,
            "levels": {
                level: LevelResult(
                    level=level, round=1, generation_attempt=1, status="passed", prompt=prompt
                )
                for level, prompt in OUTPUTS.items()
            },
            "pending": ["rich"],
            "feedback": {"rich": "Revise lighting."},
            "reason": "",
            "group_validation": validation,
            "group_decision": group_decision(validation, SOURCE, OUTPUTS, caller.config.piu),
        }
        state.update(await steps.dispatch(state))
        caller.responses[("validate_group", None)] = [TechnicalFailure("budget exhausted")]
        state.update(await steps.validate_group(state))
        self.assertEqual(await steps.after_group(state), "reject")
        result = (await steps.reject(state))["result"]
        self.assertIsNone(result.details.group_validation)
        self.assertIsNone(result.details.group_decision)
        self.assertEqual(result.details.feedback, {"rich": "Revise lighting."})

    async def test_level_replacement_clears_previous_result(self):
        caller = ScriptedCaller()
        caller.responses[("validate_english", "rich")] = [TechnicalFailure("budget exhausted")]
        previous = LevelResult(
            level="rich", round=1, generation_attempt=1, status="passed", prompt="Earlier text"
        )
        steps = ExpandSteps(caller)
        task = steps.level_task(
            {
                "id": "level",
                "prompt": SOURCE,
                "round": 2,
                "levels": {"rich": previous},
                "feedback": {"rich": "Revise."},
            },
            "rich",
        )
        task["result"] = previous
        result = (await build_level(steps).ainvoke(task))["result"]
        self.assertEqual(result.status, "technical_failed")
        self.assertEqual(result.prompt, OUTPUTS["rich"])
        self.assertEqual(result.generation_attempt, 2)
        self.assertIsNone(result.validation)
        self.assertIsNone(result.decision)
