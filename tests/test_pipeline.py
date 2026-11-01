import asyncio
import json
import unittest
from collections import Counter
from unittest.mock import patch

import yaml

from pairdata.cli import create_parser
from pairdata.config import ConfigurationError
from pairdata.runner import run_command
from pairdata.storage import read_jsonl
from tests.fakes import FakeModel, RecipeFixture, response


class PipelineTest(RecipeFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Match the ordinary event loop used by the CLI.
        asyncio.get_running_loop().set_debug(False)

    def args(self, name="run", count=1):
        return create_parser().parse_args(
            [
                "run",
                "--recipe",
                str(self.path),
                "--count",
                str(count),
                "--out",
                str(self.root / name),
            ]
        )

    async def test_execution_error_survives_export_and_preview_failures(self):
        original = ConfigurationError("service rejected the request")

        async def transport(messages, schema):
            raise original

        client = FakeModel(transport)
        args = self.args()
        with (
            patch("pairdata.runner.export_run", side_effect=OSError("export unavailable")),
            patch("pairdata.runner.write_preview", side_effect=OSError("preview unavailable")),
            self.assertRaises(ConfigurationError) as caught,
        ):
            await run_command(args, self.recipe, models=client.models(self.recipe))
        self.assertIs(caught.exception, original)
        self.assertIn("Could not export results: OSError", original.__notes__)
        self.assertIn("Could not write preview: OSError", original.__notes__)
        manifest = json.loads((args.out / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "stopped")
        self.assertEqual(manifest["error"], str(original))

    async def test_preview_failure_can_resume_without_model_calls(self):
        async def transport(messages, schema):
            return response(schema["title"])

        client = FakeModel(transport)
        args = self.args()
        with patch("pairdata.runner.write_preview", side_effect=OSError("preview unavailable")):
            with self.assertRaisesRegex(OSError, "preview unavailable"):
                await run_command(args, self.recipe, models=client.models(self.recipe))
        args.resume = True
        client.invoke.reset_mock()
        statistics = await run_command(args, self.recipe, models=client.models(self.recipe))
        client.invoke.assert_not_awaited()
        self.assertEqual(statistics["passed"], 1)
        self.assertTrue((args.out / "preview.html").exists())

    async def test_target_regeneration_preserves_feedback_and_resume_budget(self):
        args = self.args()
        started = asyncio.Event()
        counts = Counter()
        target_messages = []

        async def transport(messages, schema):
            stage = schema["title"]
            counts[stage] += 1
            if stage == "target_generate":
                target_messages.append(messages)
                if counts[stage] == 2:
                    started.set()
                    await asyncio.Future()
            if stage == "target_evaluate" and counts[stage] == 1:
                return response(
                    stage,
                    {"acceptable": False, "reason": "Fix the factual omission"},
                )
            return response(stage)

        client = FakeModel(transport)
        task = asyncio.create_task(
            run_command(args, self.recipe, models=client.models(self.recipe))
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        args.resume = True
        client.invoke.reset_mock()
        stats = await run_command(args, self.recipe, models=client.models(self.recipe))
        self.assertEqual(client.invoke.await_count, 2)
        self.assertEqual(counts["source_generate"], 1)
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["technical_retries"], 1)
        self.assertEqual(stats["quality_retries"], 1)
        self.assertEqual(len(target_messages), 3)
        self.assertEqual(target_messages[1], target_messages[2])
        self.assertNotIn("Fix the factual omission", json.dumps(target_messages[0]))
        self.assertIn("Fix the factual omission", json.dumps(target_messages[1]))
        events = list(read_jsonl(args.out / "audit.jsonl"))
        self.assertEqual(sum(event["event"] == "sample" for event in events), 2)
        target_inputs = [
            event["data"]["input"]["inputs"]
            for event in events
            if event["event"] == "request" and event["stage"] == "target.generate"
        ]
        self.assertEqual(set(target_inputs[0]), {"source", "sample", "previous", "feedback"})
        self.assertEqual(set(target_inputs[0]["sample"]), {"tone"})
        self.assertIsNone(target_inputs[0]["previous"])
        self.assertIsNone(target_inputs[0]["feedback"])
        self.assertEqual(target_inputs[1], target_inputs[2])
        self.assertEqual(target_inputs[1]["previous"], "The meeting starts at 10.")
        self.assertFalse(target_inputs[1]["feedback"]["evaluation"]["acceptable"])
        self.assertFalse(target_inputs[1]["feedback"]["decision"]["passed"])
        for item in target_inputs[1:]:
            self.assertEqual(item["source"], target_inputs[0]["source"])
            self.assertEqual(item["sample"], target_inputs[0]["sample"])

    async def test_completed_run_rebuilds_exports_without_model_calls(self):
        async def transport(messages, schema):
            return response(schema["title"])

        client = FakeModel(transport)
        args = self.args()
        stats = await run_command(args, self.recipe, models=client.models(self.recipe))
        args.resume = True
        expected = (args.out / "data.jsonl").read_bytes()
        self.assertEqual(set(json.loads(expected)), {"source", "target"})
        (args.out / "data.jsonl").write_text("damaged derived export")
        client.invoke.reset_mock()
        self.assertEqual(
            await run_command(args, self.recipe, models=client.models(self.recipe)), stats
        )
        client.invoke.assert_not_awaited()
        self.assertEqual((args.out / "data.jsonl").read_bytes(), expected)
        self.assertNotIn("test-secret", (args.out / "recipe.json").read_text())
        self.assertNotIn("test-secret", (args.out / "manifest.json").read_text())
        sampler_path = self.path.parent / self.recipe.settings.target.sampler
        sampler = yaml.safe_load(sampler_path.read_text())
        sampler["tone"]["choices"][0]["weight"] = 2
        sampler_path.write_text(yaml.safe_dump(sampler))
        with self.assertRaisesRegex(ValueError, "recipe mismatch"):
            await run_command(args, self.load(), models=client.models(self.recipe))

    async def test_rejection_stays_in_the_failed_stage(self):
        for failed in ("source", "target"):
            with self.subTest(failed=failed):

                async def transport(messages, schema):
                    stage = schema["title"]
                    return (
                        response(stage, {"acceptable": False, "reason": "Failed quality"})
                        if stage == f"{failed}_evaluate"
                        else response(stage)
                    )

                client = FakeModel(transport)
                args = self.args(failed)
                stats = await run_command(args, self.recipe, models=client.models(self.recipe))
                self.assertEqual(stats["rejected"], 1)
                self.assertEqual(list(read_jsonl(args.out / "data.jsonl")), [])
                rejected = next(read_jsonl(args.out / "rejected.jsonl"))
                self.assertEqual(rejected[failed]["generation_attempt"], 3)
                self.assertEqual(stats["requests"], 6 if failed == "source" else 8)
                self.assertEqual(
                    stats["distributions"]["target"]["not_reached"], 1 if failed == "source" else 0
                )
                inputs = [
                    event["data"]["input"]["inputs"]
                    for event in read_jsonl(args.out / "audit.jsonl")
                    if event["event"] == "request" and event["stage"] == f"{failed}.generate"
                ]
                self.assertIsNone(inputs[0]["feedback"])
                for item in inputs[1:]:
                    self.assertEqual(item["sample"], inputs[0]["sample"])
                    self.assertEqual(item["previous"], rejected[failed]["candidate"])
                    self.assertEqual(item["feedback"]["evaluation"]["reason"], "Failed quality")

    async def test_configured_object_outputs_and_shared_concurrency(self):
        config = yaml.safe_load(self.path.read_text())
        for stage in ("source", "target"):
            config[stage]["generate"]["output_path"] = "payload[0]"
            schema_path = self.path.parent / f"schemas/{stage}_generate.json"
            schema_path.write_text(
                json.dumps(
                    {
                        "title": f"{stage}_generate",
                        "type": "object",
                        "properties": {
                            "payload": {"type": "array", "minItems": 1, "items": {"type": "object"}}
                        },
                        "required": ["payload"],
                        "additionalProperties": False,
                    }
                )
            )
            (self.path.parent / f"rules/{stage}.yaml").write_text(
                "field: evaluation.acceptable\nop: eq\nvalue: true\n"
            )
        self.path.write_text(yaml.safe_dump(config))
        recipe = self.load()
        recipe.settings.runtime.concurrency = 2
        recipe.settings.runtime.preview_page_size = 1
        active = maximum = 0

        async def transport(messages, schema):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.01)
                stage = schema["title"]
                return (
                    response(stage, {"payload": [{"time": "10:00"}]})
                    if stage.endswith("generate")
                    else response(stage)
                )
            finally:
                active -= 1

        client = FakeModel(transport)
        args = self.args(count=3)
        stats = await run_command(args, recipe, models=client.models(recipe))
        self.assertEqual(stats["passed"], 3)
        self.assertEqual(maximum, 2)
        rows = list(read_jsonl(args.out / "data.jsonl"))
        self.assertEqual(rows, [{"source": {"time": "10:00"}, "target": {"time": "10:00"}}] * 3)
        self.assertTrue((args.out / "preview/page-0003.html").exists())
