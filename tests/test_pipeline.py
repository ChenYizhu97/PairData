import asyncio
import tempfile
import unittest
from pathlib import Path

from midasdata.config import ExpansionRanges
from midasdata.execution import ExecutionContext, ExecutionFailed
from midasdata.sampler import load_sampler
from midasdata.storage import RunStore
from midasdata.validation import assess_group
from midasdata.workflows.prompt_expansion import build_prompt_expansion
from midasdata.workflows.source_generation import build_source_generation
from tests.fakes import (
    PROMPTS,
    SOURCE,
    FakeModel,
    expansion_draft,
    expansion_report,
    local_settings,
    model_input,
    request_level,
    source_report,
)


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = local_settings(self.root)
        self.run_number = 0

    def execution(self):
        self.run_number += 1
        store = RunStore(self.root / str(self.run_number))
        store.open("generate", self.settings, {}, None, resume=False, run_id="test", tasks=[])
        self.addCleanup(store.close)
        return ExecutionContext(store, asyncio.Semaphore(4), 0, 0)

    async def test_source_revision_budget_and_current_evidence(self):
        invalid = source_report()
        invalid["clean"] = False
        for scenario in ("exhausted", "replacement_failed", "validator_failed"):
            with self.subTest(scenario=scenario):
                model = FakeModel()
                model.responses[("blind_sampler_validation", None)] = [invalid] * 3
                if scenario != "exhausted":
                    model.responses[("generate", None)] = [
                        {"concept": "first", "prompt": SOURCE},
                        ExecutionFailed("offline")
                        if scenario == "replacement_failed"
                        else {"concept": "second", "prompt": "两只猫"},
                    ]
                    model.responses[("blind_sampler_validation", None)] = [
                        invalid,
                        ExecutionFailed("offline"),
                    ]
                result = (
                    await build_source_generation(
                        model, model, self.execution(), load_sampler()
                    ).ainvoke(
                        {
                            "task_id": scenario,
                            "conditions": {},
                        }
                    )
                )["result"]
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.records, [])
                if scenario == "exhausted":
                    self.assertEqual(result.details.generation_attempt, 3)
                    self.assertEqual(result.reason, "quality_budget_exhausted")
                elif scenario == "replacement_failed":
                    self.assertEqual(result.details.prompt, SOURCE)
                    self.assertIsNotNone(result.details.validation)
                else:
                    self.assertEqual(result.details.prompt, "两只猫")
                    self.assertIsNone(result.details.validation)
                    self.assertIsNone(result.details.decision)

    async def test_failed_levels_regenerate_with_fixed_targets(self):
        for exhaust in (False, True):
            with self.subTest(exhaust=exhaust):
                model = FakeModel()
                semantics = expansion_report("rich")
                semantics.update(passed=False, reason="Restore the cat.")
                too_few = expansion_report("rich")
                too_few["added_pius"].pop()
                model.responses[("blind_expand_validation", "rich")] = [semantics, too_few]
                if exhaust:
                    model.responses[("blind_expand_validation", "rich")].append(too_few)
                execution = self.execution()
                result = (
                    await build_prompt_expansion(
                        model, model, execution, ExpansionRanges()
                    ).ainvoke({"task_id": "budget", "source_prompt": SOURCE})
                )["result"]
                self.assertEqual(
                    {
                        level: item.generation_attempt
                        for level, item in result.details.levels.items()
                    },
                    {"light": 1, "medium": 1, "rich": 3},
                )
                self.assertEqual(result.status, "rejected" if exhaust else "passed")
                if exhaust:
                    self.assertEqual(result.reason, "quality_budget_exhausted")
                    self.assertEqual(result.records, [])
                    self.assertIsNone(result.details.group_decision)
                else:
                    self.assertEqual(
                        [item.prompt for item in result.records], list(PROMPTS.values())
                    )
                    self.assertTrue(result.details.group_decision.passed)
                generation_inputs = {}
                for stage, payload in model.calls:
                    if stage == "expand":
                        self.assertEqual(set(payload), {"source", "target_added_pius"})
                        level = request_level(stage, payload)
                        interval = getattr(ExpansionRanges(), level)
                        target = payload["target_added_pius"]
                        self.assertIs(type(target), int)
                        self.assertLessEqual(interval.min, target)
                        self.assertLessEqual(target, interval.max)
                        self.assertEqual(payload, generation_inputs.setdefault(level, payload))
                    elif stage == "blind_expand_validation":
                        self.assertEqual(set(payload), {"source", "output"})
                    else:
                        self.fail(f"unexpected model stage: {stage}")
                self.assertFalse(
                    any(
                        event["stage"] == "group_check" and event["event"] == "request"
                        for event in execution.store.events
                    )
                )

    async def test_group_check_requires_all_three_passed_levels(self):
        model = FakeModel()
        execution = self.execution()
        result = (
            await build_prompt_expansion(model, model, execution, ExpansionRanges()).ainvoke(
                {"task_id": "group", "source_prompt": SOURCE}
            )
        )["result"]
        levels = result.details.levels
        self.assertTrue(assess_group(levels).passed)
        self.assertEqual(len(model.calls), 6)
        self.assertTrue(
            any(item.target_added_pius != item.decision.added_count for item in levels.values())
        )
        for stage, payload in model.calls:
            if stage == "expand":
                level = request_level(stage, payload)
                self.assertEqual(levels[level].target_added_pius, payload["target_added_pius"])
        for level in PROMPTS:
            missing = {key: value for key, value in levels.items() if key != level}
            self.assertFalse(assess_group(missing).passed)
            failed = {
                **levels,
                level: levels[level].model_copy(update={"status": "quality_failed"}),
            }
            self.assertFalse(assess_group(failed).passed)
            no_decision = {**levels, level: levels[level].model_copy(update={"decision": None})}
            self.assertFalse(assess_group(no_decision).passed)
        self.assertEqual(
            sum(event["stage"] == "group_check" for event in execution.store.events), 1
        )
        configured = ExpansionRanges(medium={"min": 2, "max": 2}, rich={"min": 5, "max": 5})
        configured_result = (
            await build_prompt_expansion(model, model, self.execution(), configured).ainvoke(
                {"task_id": "configured", "source_prompt": SOURCE}
            )
        )["result"]
        self.assertEqual(configured_result.status, "passed")
        self.assertEqual(
            {
                level: item.target_added_pius
                for level, item in configured_result.details.levels.items()
            },
            {"light": 0, "medium": 2, "rich": 5},
        )

    async def test_failed_attempt_retains_only_current_draft_evidence(self):
        for failure in ("generation", "validation"):
            with self.subTest(failure=failure):
                model = FakeModel()
                invalid = expansion_report("medium")
                invalid.update(passed=False, reason="Restore the source.")
                model.responses[("blind_expand_validation", "medium")] = [invalid]
                if failure == "generation":
                    model.responses[("expand", "medium")] = [
                        expansion_draft("medium"),
                        ExecutionFailed("offline"),
                    ]
                else:
                    model.responses[("blind_expand_validation", "medium")].append(
                        ExecutionFailed("offline")
                    )
                result = (
                    await build_prompt_expansion(
                        model, model, self.execution(), ExpansionRanges()
                    ).ainvoke({"task_id": failure, "source_prompt": SOURCE})
                )["result"]
                self.assertEqual(result.status, "rejected")
                self.assertIsNone(result.details.group_decision)
                attempt = result.details.levels["medium"]
                self.assertIsNone(attempt.validation)
                self.assertIsNone(attempt.decision)
                self.assertEqual(attempt.status, "technical_failed")
                self.assertEqual(
                    attempt.prompt, "" if failure == "generation" else PROMPTS["medium"]
                )
                self.assertEqual(
                    sum(
                        stage == "blind_expand_validation"
                        and request_level(stage, payload) == "medium"
                        for stage, payload in model.calls
                    ),
                    1 if failure == "generation" else 2,
                )

    async def test_parallel_attempts_join_before_group_check(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingModel(FakeModel):
            async def invoke(self, inputs, schema):
                stage, payload = model_input(inputs)
                if stage == "expand" and request_level(stage, payload) == "medium":
                    started.set()
                    await release.wait()
                return await super().invoke(inputs, schema)

        model = BlockingModel()
        execution = self.execution()
        task = asyncio.create_task(
            build_prompt_expansion(model, model, execution, ExpansionRanges()).ainvoke(
                {"task_id": "parallel", "source_prompt": SOURCE}
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(30):
                if sum(stage == "blind_expand_validation" for stage, _ in model.calls) == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(sum(stage == "blind_expand_validation" for stage, _ in model.calls), 2)
            self.assertFalse(
                any(event["stage"] == "group_check" for event in execution.store.events)
            )
            self.assertFalse(task.done())
            release.set()
            self.assertEqual((await asyncio.wait_for(task, 3))["result"].status, "passed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
