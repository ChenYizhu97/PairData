import asyncio
import copy
import json
import os
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import openai
from langchain_core.messages import AIMessage

from midasdata.config import ConfigurationError, ExpansionRanges
from midasdata.execution import ExecutionContext, ExecutionFailed, execute, operation_metadata
from midasdata.model import LLMModel
from midasdata.schemas import ExpansionDraft, ExpansionResult, ExpansionValidation
from midasdata.storage import RunStore, write_jsonl
from midasdata.validation import assess_expansion
from midasdata.workflows.prompt_expansion import validate_expansion_set
from tests.fakes import PROMPTS, SOURCE, expansion_report, local_settings, set_report


class ExecutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.settings = local_settings(self.directory)
        self.store = RunStore(self.directory / "run")
        self.store.open(
            "generate", self.settings, {"count": 0}, None, resume=False, run_id="test", tasks=[]
        )
        self.addCleanup(lambda: self.store.close())
        self.execution = ExecutionContext(self.store, asyncio.Semaphore(2), 0, 0)
        self.model = LLMModel(self.settings.generator, api_key="test-secret", timeout=3)
        self.transport = AsyncMock()
        self.transport.ainvoke.return_value = AIMessage(content='{"prompt":"A cat."}')
        self.model.client = self.transport

    def resume(self):
        self.store.close()
        self.store = RunStore(self.directory / "run")
        self.store.open(
            "generate", self.settings, {"count": 0}, None, resume=True, run_id="unused", tasks=[]
        )
        self.execution = ExecutionContext(self.store, asyncio.Semaphore(2), 0, 0)

    async def invoke_model(self, task_id):
        return await execute(
            partial(self.model.invoke, [], ExpansionDraft),
            context=self.execution,
            metadata=operation_metadata(task_id, "expand", model=self.model.name),
            payload={},
            parse=partial(self.model.parse, schema=ExpansionDraft),
            retry_if=self.model.is_retryable,
            describe_error=self.model.error_text,
            max_attempts=5,
        )

    async def test_json_retry_and_native_response_use_the_same_schema(self):
        self.transport.ainvoke.side_effect = [
            AIMessage(content="invalid"),
            AIMessage(content='{"prompt":"A cat."}'),
        ]
        response = await self.invoke_model("json")
        self.assertEqual(response.prompt, "A cat.")
        self.assertEqual(self.transport.ainvoke.await_count, 2)
        self.model.structured_output = "native"
        native = AsyncMock()
        native.ainvoke.return_value = {
            "raw": AIMessage(content="{}"),
            "parsed": ExpansionDraft(prompt="A cat."),
        }
        self.transport.with_structured_output = Mock(return_value=native)
        response = await self.invoke_model("native")
        self.assertEqual(response.prompt, "A cat.")
        self.assertEqual(native.ainvoke.await_count, 1)

    async def test_non_model_operation_recovery_and_remaining_attempts(self):
        # 普通操作也使用相同的恢复机制，不依赖模型或 Pydantic 响应。
        # Ordinary operations share recovery without model or Pydantic response dependencies.
        for outcome in ("success", "raw_only", "unknown", "remaining"):
            with self.subTest(outcome=outcome):
                operation = AsyncMock(return_value={"value": "saved"})
                metadata = operation_metadata(outcome, "transform")

                async def run(payload=None):
                    return await execute(
                        operation,
                        context=self.execution,
                        metadata=metadata,
                        payload=payload or {},
                        parse=lambda data: data["value"],
                        retry_if=lambda error: isinstance(error, TimeoutError),
                        max_attempts=5,
                    )

                if outcome in {"unknown", "remaining"}:
                    operation.side_effect = asyncio.CancelledError()
                    for _ in range(5 if outcome == "unknown" else 2):
                        with self.assertRaises(asyncio.CancelledError):
                            await run()
                else:
                    self.assertEqual(await run(), "saved")
                if outcome == "raw_only":
                    write_jsonl(
                        self.store.directory / "audit.jsonl",
                        [
                            event
                            for event in self.store.events
                            if not (event["sample_id"] == outcome and event["event"] == "success")
                        ],
                    )
                self.resume()
                operation.reset_mock()
                operation.side_effect = None
                if outcome == "unknown":
                    with self.assertRaises(ExecutionFailed):
                        await run()
                else:
                    self.assertEqual(await run(), "saved")
                    with self.assertRaises(ConfigurationError):
                        await run({"changed": True})
                if outcome == "remaining":
                    self.assertEqual(operation.await_count, 1)
                    attempts = [
                        event["technical_attempt"]
                        for event in self.store.operation_events[metadata["logical_call_id"]]
                        if event["event"] == "request"
                    ]
                    self.assertEqual(attempts, [1, 2, 3])
                else:
                    operation.assert_not_awaited()

    async def test_invalid_report_retries_then_node_assesses_quality(self):
        invalid = set_report()
        invalid["levels"]["rich"]["missing_pius"] = ["cat"]
        repairable = copy.deepcopy(invalid)
        repairable["retry_levels"], repairable["feedback"] = ["rich"], "Restore the cat."
        self.transport.ainvoke.side_effect = [
            AIMessage(content=json.dumps(value)) for value in (invalid, repairable)
        ]
        state = {
            "task_id": "contract",
            "source_prompt": SOURCE,
            "round_number": 1,
            "feedback_by_level": {},
            "expansions": {
                level: ExpansionResult(
                    level=level, round=1, generation_attempt=1, status="passed", prompt=prompt
                )
                for level, prompt in PROMPTS.items()
            },
        }
        update = await validate_expansion_set(
            state, validator=self.model, execution=self.execution, ranges=self.settings.piu
        )
        self.assertFalse(update["set_decision"].passed)
        self.assertEqual(update["pending_levels"], ["rich"])
        self.assertEqual(self.transport.ainvoke.await_count, 2)
        events = [event["event"] for event in self.store.events]
        self.assertEqual(events[-2:], ["success", "quality_decision"])
        self.assertEqual(events.count("quality_decision"), 1)
        self.transport.ainvoke.reset_mock()
        self.assertEqual(
            await validate_expansion_set(
                state, validator=self.model, execution=self.execution, ranges=self.settings.piu
            ),
            update,
        )
        self.transport.ainvoke.assert_not_awaited()
        self.assertEqual(
            sum(event["event"] == "quality_decision" for event in self.store.events), 1
        )

    async def test_fatal_provider_error_stops_without_exposing_credentials(self):
        response = httpx.Response(401, request=httpx.Request("POST", "http://127.0.0.1:1"))
        self.transport.ainvoke.side_effect = openai.AuthenticationError(
            "invalid test-secret", response=response, body=None
        )
        with self.assertRaises(ConfigurationError) as raised:
            await self.invoke_model("auth")
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertNotIn("test-secret", (self.store.directory / "audit.jsonl").read_text())
        self.assertEqual(self.transport.ainvoke.await_count, 1)

    def test_evidence_checks_override_claimed_success(self):
        valid = expansion_report("medium")
        mutations = (
            lambda report: report["preserved_pius"].clear(),
            lambda report: report["original_pius"][0].update(evidence="not in source"),
            lambda report: report["added_pius"][1].update(fact=report["added_pius"][0]["fact"]),
            lambda report: report["added_pius"][0].update(output_evidence="invented evidence"),
        )
        for mutation in mutations:
            report = copy.deepcopy(valid)
            mutation(report)
            decision = assess_expansion(
                ExpansionValidation.model_validate(report),
                SOURCE,
                PROMPTS["medium"],
                "medium",
                ExpansionRanges(),
            )
            self.assertFalse(decision.passed)
