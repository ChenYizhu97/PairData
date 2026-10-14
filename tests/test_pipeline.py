import asyncio
import tempfile
import unittest
from pathlib import Path

from midasdata.config import ExpansionRanges
from midasdata.execution import ExecutionContext, ExecutionFailed
from midasdata.sampler import load_sampler
from midasdata.storage import RunStore
from midasdata.workflows.prompt_expansion import build_prompt_expansion
from midasdata.workflows.source_generation import build_source_generation
from tests.fakes import (
    PROMPTS,
    SOURCE,
    FakeModel,
    expansion_report,
    local_settings,
    model_input,
    set_report,
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
                model.responses[("validate_source", None)] = [invalid] * 3
                if scenario != "exhausted":
                    model.responses[("generate", None)] = [
                        {"concept": "first", "prompt": SOURCE},
                        ExecutionFailed("offline")
                        if scenario == "replacement_failed"
                        else {"concept": "second", "prompt": "两只猫"},
                    ]
                    model.responses[("validate_source", None)] = [
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

    async def test_targeted_revisions_share_one_budget(self):
        for exhaust in (False, True):
            with self.subTest(exhaust=exhaust):
                model = FakeModel()
                invalid = expansion_report("rich")
                invalid["missing_pius"] = ["cat"]
                model.responses[("validate_english", "rich")] = [invalid]
                revise = set_report()
                revise["retry_levels"], revise["feedback"] = ["rich"], "Revise lighting."
                model.responses[("validate_group", None)] = [revise] * (2 if exhaust else 1)
                result = (
                    await build_prompt_expansion(
                        model, model, self.execution(), ExpansionRanges()
                    ).ainvoke(
                        {
                            "task_id": "budget",
                            "source_prompt": SOURCE,
                        }
                    )
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
                    self.assertEqual(result.reason, "group_quality_budget_exhausted")
                    self.assertEqual(result.records, [])
                else:
                    self.assertEqual(
                        [record.prompt for record in result.records], list(PROMPTS.values())
                    )
                revisions = [
                    payload
                    for stage, payload in model.calls
                    if stage == "expand" and payload["previous_output"]
                ]
                self.assertTrue(
                    all(payload["previous_output"] == PROMPTS["rich"] for payload in revisions)
                )

    async def test_insufficient_space_terminates_the_whole_set(self):
        for scope in ("individual", "set", "nested"):
            with self.subTest(scope=scope):
                model = FakeModel()
                if scope == "individual":
                    report = expansion_report("rich")
                    report["insufficient_expansion_space"] = True
                    model.responses[("validate_english", "rich")] = [report]
                else:
                    report = set_report()
                    target = report if scope == "set" else report["levels"]["rich"]
                    target["insufficient_expansion_space"] = True
                    model.responses[("validate_group", None)] = [report]
                result = (
                    await build_prompt_expansion(
                        model, model, self.execution(), ExpansionRanges()
                    ).ainvoke(
                        {
                            "task_id": scope,
                            "source_prompt": SOURCE,
                        }
                    )
                )["result"]
                self.assertEqual(result.reason, "insufficient_expansion_space")
                self.assertEqual(result.records, [])
                self.assertEqual(sum(stage == "expand" for stage, _ in model.calls), 3)
                if scope == "individual":
                    self.assertFalse(any(stage == "validate_group" for stage, _ in model.calls))

    async def test_new_round_clears_old_set_evidence_and_skips_failed_draft_validation(self):
        model = FakeModel()
        revise = set_report()
        revise["retry_levels"], revise["feedback"] = ["medium"], "Revise the setting."
        model.responses[("validate_group", None)] = [revise]
        model.responses[("expand", "medium")] = [
            {"prompt": PROMPTS["medium"]},
            ExecutionFailed("offline"),
        ]
        result = (
            await build_prompt_expansion(model, model, self.execution(), ExpansionRanges()).ainvoke(
                {
                    "task_id": "failure",
                    "source_prompt": SOURCE,
                }
            )
        )["result"]
        self.assertEqual(result.status, "rejected")
        self.assertIsNone(result.details.group_validation)
        self.assertIsNone(result.details.group_decision)
        self.assertIsNone(result.details.levels["medium"].validation)
        self.assertEqual(
            sum(
                stage == "validate_english" and payload["level"] == "medium"
                for stage, payload in model.calls
            ),
            1,
        )

    async def test_parallel_attempts_join_before_set_validation(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingModel(FakeModel):
            async def invoke(self, inputs, schema):
                stage, payload = model_input(inputs)
                if stage == "expand" and payload["level"] == "medium":
                    started.set()
                    await release.wait()
                return await super().invoke(inputs, schema)

        model = BlockingModel()
        task = asyncio.create_task(
            build_prompt_expansion(model, model, self.execution(), ExpansionRanges()).ainvoke(
                {
                    "task_id": "parallel",
                    "source_prompt": SOURCE,
                }
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(30):
                if sum(stage == "validate_english" for stage, _ in model.calls) == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(sum(stage == "validate_english" for stage, _ in model.calls), 2)
            self.assertFalse(any(stage == "validate_group" for stage, _ in model.calls))
            release.set()
            self.assertEqual((await asyncio.wait_for(task, 3))["result"].status, "passed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
