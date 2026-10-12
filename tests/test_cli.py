"""Generate, expand, export, preview, and resume with a controlled model transport."""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from midasdata.cli import execute, parser
from midasdata.config import load_config
from midasdata.io import read_jsonl
from tests.support import OUTPUTS, SOURCE, english_validation, expansion_case, source_validation


class CliTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_level_revision_reuses_calls_and_preserves_budgets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            env = root / "settings.env"
            env.write_text("model=test-model\napi_key=test-secret\n")
            config = load_config(None, {"retry_initial": 0.0, "retry_max": 0.0}, env)
            source = root / "input.jsonl"
            source.write_text(json.dumps({"id": "sample", "prompt": SOURCE}) + "\n")
            args = parser().parse_args(
                ["expand", "--input", str(source), "--out", str(root / "run")]
            )
            medium_generations = 0
            revision_started = asyncio.Event()

            async def response(inputs):
                nonlocal medium_generations
                schema = json.loads(inputs[0]["content"].rsplit("formatting:\n", 1)[1])
                payload = json.loads(inputs[1]["content"])
                if schema["title"] == "EnglishDraft":
                    if payload["level"] == "medium":
                        medium_generations += 1
                        if medium_generations == 2:
                            revision_started.set()
                            await asyncio.Future()
                    value = {"prompt": OUTPUTS[payload["level"]]}
                elif schema["title"] == "EnglishValidation":
                    value = english_validation(payload["level"])
                    if payload["level"] == "medium" and medium_generations == 1:
                        value["missing_pius"] = ["cat"]
                        value["feedback"] = "Preserve the cat."
                elif schema["title"] == "GroupValidation":
                    value = expansion_case()["validation"]
                else:
                    raise AssertionError(schema["title"])
                return AIMessage(content=json.dumps(value))

            with patch("midasdata.llm.ChatOpenAI") as factory:
                factory.return_value.ainvoke = AsyncMock(side_effect=response)
                running = asyncio.create_task(execute(args, config))
                try:
                    await asyncio.wait_for(revision_started.wait(), timeout=5)
                finally:
                    running.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await running
                manifest = json.loads((args.out / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "interrupted")
                self.assertEqual(manifest["statistics"]["pending"], 1)

                args.resume = True
                factory.return_value.ainvoke.reset_mock()
                statistics = await execute(args, config)
                self.assertEqual(factory.return_value.ainvoke.await_count, 3)
                self.assertEqual(statistics["passed"], 1)
                self.assertEqual(statistics["requests"], 10)
                self.assertEqual(statistics["logical_generations"], 4)
                self.assertEqual(statistics["quality_retries"], 1)
                self.assertEqual(statistics["technical_retries"], 1)
                self.assertEqual(len(list(read_jsonl(args.out / "sft.jsonl"))), 3)

    async def test_fresh_generate_expand_and_resume(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            env = root / "settings.env"
            env.write_text(
                "model=test-model\nbase_url=http://127.0.0.1:1/v1\napi_key=test-secret\n"
            )
            config = load_config(None, {}, env)

            async def response(inputs):
                schema = json.loads(inputs[0]["content"].rsplit("formatting:\n", 1)[1])
                payload = json.loads(inputs[1]["content"])
                title = schema["title"]
                if title == "SourceDraft":
                    text = {"en": "A cat.", "mixed": "一只 orange cat"}.get(
                        payload["sample"].get("language"), SOURCE
                    )
                    value = {"concept": "猫", "prompt": text}
                elif title == "SourceValidation":
                    value = source_validation(payload["sample"], payload["prompt"])
                elif title == "EnglishDraft":
                    value = {"prompt": OUTPUTS[payload["level"]]}
                elif title == "EnglishValidation":
                    value = english_validation(payload["level"], payload["source"])
                elif title == "GroupValidation":
                    value = expansion_case(payload["source"])["validation"]
                else:
                    raise AssertionError(title)
                return AIMessage(
                    content=json.dumps(value),
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )

            with patch("midasdata.llm.ChatOpenAI") as factory:
                model = factory.return_value
                model.ainvoke = AsyncMock(side_effect=response)
                generated = parser().parse_args(
                    ["generate", "--count", "2", "--out", str(root / "generated")]
                )
                statistics = await execute(generated, config)
                self.assertEqual(statistics["passed"], 2)
                self.assertEqual(statistics["requests"], 4)
                source = generated.out / "data.jsonl"
                source_rows = list(read_jsonl(source))
                self.assertEqual(len(source_rows), 2)
                expanded = parser().parse_args(
                    ["expand", "--input", str(source), "--out", str(root / "expanded")]
                )
                statistics = await execute(expanded, config)
                self.assertEqual(statistics["passed"], 2)
                self.assertEqual(statistics["requests"], 14)
                data = list(read_jsonl(expanded.out / "data.jsonl"))
                self.assertEqual(
                    [row["id"] for row in data],
                    [row["id"] for row in source_rows for _ in range(3)],
                )
                sft = list(read_jsonl(expanded.out / "sft.jsonl"))
                self.assertEqual(len(sft), 6)
                self.assertEqual(
                    [
                        row["messages"][1]["content"].split("Source description: ", 1)[1]
                        for row in sft
                    ],
                    [row["prompt"] for row in source_rows for _ in range(3)],
                )
                preview = (expanded.out / "preview.html").read_text()
                self.assertEqual(
                    len(json.loads(re.search(r'id="data">(.*?)</script>', preview, re.S)[1])), 2
                )
                manifest = json.loads((expanded.out / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "complete")
                self.assertNotIn("test-secret", json.dumps(manifest))
                expanded.resume = True
                model.ainvoke.reset_mock()
                model.ainvoke.side_effect = AssertionError(
                    "committed samples should not call a model"
                )
                self.assertEqual(await execute(expanded, config), statistics)
                model.ainvoke.assert_not_awaited()
                self.assertEqual(list(read_jsonl(expanded.out / "sft.jsonl")), sft)
                self.assertEqual(dict(os.environ), {})


class EntryPointTest(unittest.TestCase):
    def test_module_entry_point_works_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [sys.executable, "-m", "midasdata", "generate", "--help"],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("--count", process.stdout)
