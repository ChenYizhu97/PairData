import asyncio
import json
import unittest
from functools import partial
from unittest.mock import AsyncMock, Mock

import httpx
import openai
from langchain_core.messages import AIMessage

from pairdata.config import ConfigurationError
from pairdata.execution import ExecutionContext, ExecutionFailed, execute, operation_metadata
from pairdata.model import LLMModel
from pairdata.storage import RunStore, write_jsonl
from tests.fakes import RecipeFixture


class ExecutionTest(RecipeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store = RunStore(self.root / "run")
        self.store.open(
            self.recipe.snapshot(), {"count": 0, "seed": 42}, resume=False, run_id="test", tasks=[]
        )
        self.addCleanup(lambda: self.store.close())
        self.context = ExecutionContext(self.store, asyncio.Semaphore(2), 0, 0)

    def resume(self):
        self.store.close()
        self.store = RunStore(self.root / "run")
        self.store.open(
            self.recipe.snapshot(), {"count": 0, "seed": 42}, resume=True, run_id="unused", tasks=[]
        )
        self.context.store = self.store

    async def test_schema_retry_raw_recovery_and_unknown_request_budget(self):
        operation = AsyncMock(side_effect=[{"raw": "bad JSON"}, {"raw": '{"text":"Saved"}'}])

        async def run(task_id, payload=None):
            return await execute(
                operation,
                context=self.context,
                metadata=operation_metadata(task_id, "source.generate"),
                payload=payload or {},
                parse=self.recipe.stages["source"].generate.validator.parse,
                retry_if=LLMModel.is_retryable,
                max_attempts=2,
            )

        self.assertEqual(await run("saved"), {"text": "Saved"})
        self.assertEqual(operation.await_count, 2)
        self.assertTrue(
            any(
                event["event"] == "response" and event["data"].get("raw") == "bad JSON"
                for event in self.store.events
            )
        )
        write_jsonl(
            self.store.directory / "audit.jsonl",
            [event for event in self.store.events if event["event"] != "success"],
        )
        self.resume()
        operation.reset_mock()
        self.assertEqual(await run("saved"), {"text": "Saved"})
        operation.assert_not_awaited()
        with self.assertRaises(ConfigurationError):
            await run("saved", {"changed": True})
        operation.side_effect = asyncio.CancelledError()
        for _ in range(2):
            with self.assertRaises(asyncio.CancelledError):
                await run("unknown")
        self.resume()
        operation.reset_mock()
        with self.assertRaises(ExecutionFailed):
            await run("unknown")
        operation.assert_not_awaited()

    async def test_native_output_validation_and_fatal_error_redaction(self):
        model = LLMModel(self.recipe.settings.models["generator"], api_key="test-secret", timeout=3)
        transport = Mock()
        native = AsyncMock()
        native.ainvoke.return_value = {"raw": AIMessage(content="{}"), "parsed": {"text": "Native"}}
        transport.with_structured_output.return_value = native
        model.client = transport
        model.structured_output = "native"
        validator = self.recipe.stages["source"].generate.validator
        result = await model.invoke([], validator.schema)
        self.assertEqual(validator.parse(result), {"text": "Native"})
        with self.assertRaises(ValueError):
            validator.parse({"parsed": {"text": 12}})
        http_response = httpx.Response(401, request=httpx.Request("POST", "http://127.0.0.1:1"))
        native.ainvoke.side_effect = openai.AuthenticationError(
            "invalid test-secret", response=http_response, body=None
        )
        with self.assertRaises(ConfigurationError):
            await execute(
                partial(model.invoke, [], validator.schema),
                context=self.context,
                metadata=operation_metadata("auth", "source.generate"),
                payload={},
                parse=validator.parse,
                retry_if=model.is_retryable,
                describe_error=model.error_text,
                max_attempts=5,
            )
        self.assertNotIn("test-secret", json.dumps(self.store.events))
        self.assertEqual(self.store.events[-1]["event"], "fatal")
