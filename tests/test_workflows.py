"""Current graphs operate on fresh inputs and respect shared revision budgets."""

import unittest
from unittest.mock import patch

from midasdata.llm import TechnicalFailure
from midasdata.sampler import load_sampler
from midasdata.schemas import LEVELS, ExpandDetails
from midasdata.validation import group_decision
from midasdata.workflows.expand import build_expand
from midasdata.workflows.generate import build_generate
from tests.support import OUTPUTS, SOURCE, ScriptedCaller, english_validation, expansion_case


class WorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_generate_graph_reuse_keeps_samples_independent(self):
        caller = ScriptedCaller()
        graph = build_generate(caller, load_sampler())
        for sample_id in ("first", "second"):
            result = (await graph.ainvoke({"id": sample_id, "sample": {"language": "zh"}}))[
                "result"
            ]
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.records[0].prompt, SOURCE)
            self.assertEqual(result.details.generation_attempt, 1)
        drafts = [call for call in caller.calls if call["stage"] == "generate"]
        self.assertEqual([call["generation_attempt"] for call in drafts], [1, 1])
        self.assertTrue(all(call["payload"]["previous_prompt"] == "" for call in drafts))

    async def test_source_quality_budget_stops_after_three_generations(self):
        caller = ScriptedCaller()
        invalid = {
            "conditions": {},
            "visual": False,
            "clean": True,
            "visual_evidence": SOURCE,
            "feedback": "Add visual content.",
        }
        caller.responses[("validate_source", None)] = [invalid] * 3
        result = (
            await build_generate(caller, load_sampler()).ainvoke({"id": "source", "sample": {}})
        )["result"]
        self.assertEqual(result.reason, "quality_budget_exhausted")
        self.assertEqual(result.details.generation_attempt, 3)
        self.assertEqual(result.records, [])

    async def test_expand_graph_reuse_and_group_assessment(self):
        caller = ScriptedCaller()
        graph = build_expand(caller)
        with patch("midasdata.workflows.steps.group_decision", wraps=group_decision) as assess:
            for sample_id in ("first", "second"):
                result = (await graph.ainvoke({"id": sample_id, "prompt": SOURCE}))["result"]
                self.assertEqual(result.status, "passed")
                self.assertIsInstance(result.details, ExpandDetails)
                self.assertEqual([row.level for row in result.records], list(LEVELS))
                self.assertEqual({row.level: row.prompt for row in result.records}, OUTPUTS)
                self.assertEqual(result.details.round, 1)
            self.assertEqual(assess.call_count, 2)
        for sample_id in ("first", "second"):
            calls = [
                call
                for call in caller.calls
                if call["sample_id"] == sample_id and call["stage"] == "expand"
            ]
            self.assertEqual({call["level"] for call in calls}, set(LEVELS))
            self.assertTrue(all(call["payload"]["previous_output"] == "" for call in calls))

    async def test_only_failing_level_is_revised(self):
        caller = ScriptedCaller()
        invalid = english_validation("medium")
        invalid["missing_pius"] = ["cat"]
        caller.responses[("validate_english", "medium")] = [invalid]
        result = (await build_expand(caller).ainvoke({"id": "revise", "prompt": SOURCE}))["result"]
        self.assertEqual(result.status, "passed")
        self.assertEqual(
            {level: item.generation_attempt for level, item in result.details.levels.items()},
            {"light": 1, "medium": 2, "rich": 1},
        )
        revisions = [
            call
            for call in caller.calls
            if call["stage"] == "expand" and call["generation_attempt"] == 2
        ]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["payload"]["previous_output"], OUTPUTS["medium"])

    async def test_group_and_level_revisions_share_budget(self):
        caller = ScriptedCaller()
        invalid = english_validation("rich")
        invalid["missing_pius"] = ["cat"]
        caller.responses[("validate_english", "rich")] = [invalid]
        revise = expansion_case()["validation"]
        revise["retry_levels"], revise["feedback"] = ["rich"], "Revise the lighting."
        caller.responses[("validate_group", None)] = [revise, revise]
        result = (await build_expand(caller).ainvoke({"id": "budget", "prompt": SOURCE}))["result"]
        self.assertEqual(result.reason, "group_quality_budget_exhausted")
        self.assertEqual(result.details.levels["rich"].generation_attempt, 3)
        self.assertEqual(result.records, [])

    async def test_shortage_at_either_group_scope_terminates(self):
        for scope in ("group", "rich"):
            with self.subTest(scope=scope):
                caller = ScriptedCaller()
                validation = expansion_case()["validation"]
                target = validation if scope == "group" else validation["levels"][scope]
                target["insufficient_expansion_space"] = True
                caller.responses[("validate_group", None)] = [validation]
                result = (await build_expand(caller).ainvoke({"id": scope, "prompt": SOURCE}))[
                    "result"
                ]
                self.assertEqual(result.reason, "insufficient_expansion_space")
                self.assertEqual(result.records, [])
                self.assertTrue(result.details.group_decision.insufficient_expansion_space)
                self.assertEqual(sum(call["stage"] == "expand" for call in caller.calls), 3)

    async def test_level_shortage_terminates_before_group_check(self):
        caller = ScriptedCaller()
        validation = english_validation("rich")
        validation["insufficient_expansion_space"] = True
        caller.responses[("validate_english", "rich")] = [validation]
        result = (await build_expand(caller).ainvoke({"id": "shortage", "prompt": SOURCE}))[
            "result"
        ]
        self.assertEqual(result.reason, "insufficient_expansion_space")
        self.assertFalse(any(call["stage"] == "validate_group" for call in caller.calls))

    async def test_generation_technical_failure_skips_level_validator(self):
        caller = ScriptedCaller()
        caller.responses[("expand", "medium")] = [TechnicalFailure("unavailable")]
        result = (await build_expand(caller).ainvoke({"id": "failure", "prompt": SOURCE}))["result"]
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.records, [])
        self.assertFalse(
            any(
                call["stage"] == "validate_english" and call["level"] == "medium"
                for call in caller.calls
            )
        )
