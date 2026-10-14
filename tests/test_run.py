import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from midasdata.cli import create_parser
from midasdata.config import load_settings
from midasdata.runner import run_command
from midasdata.schemas import BlindExpansionValidation, ExpansionDraft
from midasdata.storage import RunStore, read_jsonl, write_json
from tests.fakes import (
    PROMPTS,
    SOURCE,
    expansion_report,
    local_settings,
    request_level,
    transport_response,
)


class RunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = local_settings(self.root)

    async def test_generate_expand_and_committed_resume_rebuild_exports(self):
        self.settings.preview_page_size = 1
        generated = create_parser().parse_args(
            [
                "generate",
                "--count",
                "2",
                "--out",
                str(self.root / "generated"),
            ]
        )
        with patch("midasdata.model.ChatOpenAI") as factory:
            model = factory.return_value
            model.ainvoke = AsyncMock(side_effect=transport_response)
            self.assertEqual((await run_command(generated, self.settings))["passed"], 2)
            source_records = list(read_jsonl(generated.out / "data.jsonl"))
            expanded = create_parser().parse_args(
                [
                    "expand",
                    "--input",
                    str(generated.out / "data.jsonl"),
                    "--out",
                    str(self.root / "expanded"),
                ]
            )
            statistics = await run_command(expanded, self.settings)
            records = list(read_jsonl(expanded.out / "data.jsonl"))
            training = list(read_jsonl(expanded.out / "sft.jsonl"))
            self.assertEqual(statistics["requests"], 12)
            self.assertEqual(len(records), 6)
            self.assertEqual(len(training), 6)
            self.assertEqual(
                [record["id"] for record in records],
                [record["id"] for record in source_records for _ in PROMPTS],
            )
            self.assertEqual(
                [
                    item["messages"][1]["content"].split("Source description: ", 1)[1]
                    for item in training
                ],
                [record["prompt"] for record in source_records for _ in PROMPTS],
            )
            self.assertTrue((expanded.out / "preview" / "page-0002.html").exists())
            expected = (expanded.out / "data.jsonl").read_bytes()
            (expanded.out / "data.jsonl").write_text("damaged derived export")
            expanded.resume = True
            model.ainvoke.reset_mock()
            self.assertEqual(await run_command(expanded, self.settings), statistics)
            model.ainvoke.assert_not_awaited()
            self.assertEqual((expanded.out / "data.jsonl").read_bytes(), expected)
            self.assertNotIn("test-secret", (expanded.out / "manifest.json").read_text())

    async def test_interrupted_regeneration_keeps_targets_and_budgets(self):
        source = self.root / "input.jsonl"
        source.write_text(json.dumps({"id": "sample", "prompt": SOURCE}) + "\n")
        args = create_parser().parse_args(
            ["expand", "--input", str(source), "--out", str(self.root / "run")]
        )
        regeneration_started = asyncio.Event()
        medium_drafts = 0
        generation_inputs = []

        async def transport(inputs):
            nonlocal medium_drafts
            schema = json.loads(inputs[0]["content"].rsplit("formatting:\n", 1)[1])
            payload = json.loads(inputs[1]["content"])
            if (
                schema["title"] == ExpansionDraft.__name__
                and request_level("expand", payload) == "medium"
            ):
                medium_drafts += 1
                generation_inputs.append(payload)
                if medium_drafts == 2:
                    regeneration_started.set()
                    await asyncio.Future()
            if (
                schema["title"] == BlindExpansionValidation.__name__
                and request_level("blind_expand_validation", payload) == "medium"
                and medium_drafts == 1
            ):
                report = expansion_report("medium")
                report.update(passed=False, reason="Restore the cat.")
                return AIMessage(content=json.dumps(report))
            return transport_response(inputs)

        with patch("midasdata.model.ChatOpenAI") as factory:
            model = factory.return_value
            model.ainvoke = AsyncMock(side_effect=transport)
            running = asyncio.create_task(run_command(args, self.settings))
            try:
                await asyncio.wait_for(regeneration_started.wait(), 3)
            finally:
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
            self.assertEqual(
                json.loads((args.out / "manifest.json").read_text())["status"], "interrupted"
            )
            args.resume = True
            model.ainvoke.reset_mock()
            statistics = await run_command(args, self.settings)
            self.assertEqual(model.ainvoke.await_count, 2)
            self.assertEqual(statistics["logical_generations"], 4)
            self.assertEqual(statistics["technical_retries"], 1)
            self.assertEqual(statistics["quality_retries"], 1)
            self.assertEqual(statistics["passed"], 1)
            self.assertEqual(len(generation_inputs), 3)
            self.assertEqual(set(generation_inputs[0]), {"source", "target_added_pius"})
            self.assertEqual(generation_inputs, [generation_inputs[0]] * 3)

    async def test_bad_input_is_rejected_before_any_model_requests(self):
        for rows in (
            [{"id": "same", "prompt": SOURCE}, {"id": " same ", "prompt": SOURCE}],
            [{"id": "sample", "prompt": SOURCE, "unexpected": True}],
        ):
            with self.subTest(rows=rows), patch("midasdata.model.ChatOpenAI") as factory:
                source = self.root / "input.jsonl"
                source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                args = create_parser().parse_args(
                    [
                        "expand",
                        "--input",
                        str(source),
                        "--out",
                        str(self.root / f"bad-{len(rows)}"),
                    ]
                )
                with self.assertRaises(ValueError):
                    await run_command(args, self.settings)
                factory.assert_not_called()

    def test_recovery_detects_corruption_and_preserves_audit_tail_rules(self):
        for damage in ("tasks", "torn_tail", "invalid_event", "revision", "fingerprint", "locked"):
            with self.subTest(damage=damage):
                store = RunStore(self.root / damage)
                arguments = ("generate", self.settings, {"count": 1}, None)
                store.open(
                    *arguments, resume=False, run_id="test", tasks=[{"id": "x", "sample": {}}]
                )
                try:
                    if damage == "locked":
                        competing = RunStore(store.directory)
                        with self.assertRaisesRegex(ValueError, "already in use"):
                            competing.open(*arguments, resume=True, run_id="unused", tasks=[])
                        competing.close()
                        continue
                    store.close()
                    if damage == "tasks":
                        (store.directory / "tasks.jsonl").write_text("damaged")
                    elif damage in {"torn_tail", "invalid_event"}:
                        (store.directory / "audit.jsonl").write_bytes(
                            b'{"broken":' + (b"\n" if damage == "invalid_event" else b"")
                        )
                    else:
                        if damage == "revision":
                            store.manifest["workflow_revision"] = 1
                        else:
                            store.manifest["fingerprint"] = "changed"
                        write_json(store.directory / "manifest.json", store.manifest)
                    if damage == "torn_tail":
                        store.open(*arguments, resume=True, run_id="unused", tasks=[])
                        self.assertEqual((store.directory / "audit.jsonl").read_bytes(), b"")
                    else:
                        with self.assertRaises(ValueError):
                            store.open(*arguments, resume=True, run_id="unused", tasks=[])
                finally:
                    store.close()

    def test_configuration_precedence_and_credentials_are_isolated(self):
        env_file = self.root / "settings.env"
        env_file.write_text(
            "MODEL_NAME=file-model\nOPENAI_API_KEY=file-secret\nOPENAI_BASE_URL=http://127.0.0.1:1/v1\n"
        )
        with patch.dict(os.environ, {"MODEL_NAME": "process-model"}):
            before = dict(os.environ)
            first = load_settings(None, {"model": "cli-model"}, env_file)
            env_file.write_text("MODEL_NAME=other-file\nOPENAI_API_KEY=other-secret\n")
            second = load_settings(None, {}, env_file)
            self.assertEqual(first.generator.model, "cli-model")
            self.assertEqual(second.generator.model, "process-model")
            self.assertEqual(first.api_key(first.generator), "file-secret")
            self.assertEqual(second.api_key(second.generator), "other-secret")
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("file-secret", json.dumps(first.model_dump()))
