"""Model calls, retries, and recovery use only events produced by the current code."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import openai
from langchain_core.messages import AIMessage

from midasdata.config import Config, ConfigurationError, ModelConfig, load_config
from midasdata.io import RunStore, atomic_lines, encode
from midasdata.llm import ModelCaller, TechnicalFailure
from midasdata.schemas import EnglishDraft
from tests.support import SOURCE


class ModelCallsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        env = root / ".env"
        env.write_text("model=test-model\nbase_url=http://127.0.0.1:1/v1\napi_key=test-secret\n")
        self.config = load_config(None, {"retry_initial": 0.0, "retry_max": 0.0}, env)
        self.out = root / "run"
        self.store = RunStore(self.out)
        self.store.prepare("generate", self.config, {"count": 0, "seed": 0}, None, False)
        self.addCleanup(self.store.close)
        self.caller = ModelCaller(self.config, self.store)
        self.model = AsyncMock()
        self.model.ainvoke.return_value = AIMessage(content='{"prompt":"A cat."}')
        self.caller.models = {role: self.model for role in ("generator", "validator")}
        self.payload = {"source": SOURCE, "level": "light"}
        self.arguments = {
            "stage": "expand",
            "schema": EnglishDraft,
            "sample_id": "sample",
            "level": "light",
            "round": 1,
            "generation_attempt": 1,
        }

    async def call(self, **options):
        return await self.caller.call(payload=self.payload, **self.arguments, **options)

    def resume(self):
        self.store.close()
        self.store = RunStore(self.out)
        self.addCleanup(self.store.close)
        self.store.prepare("generate", self.config, {"count": 0, "seed": 0}, None, True)
        self.caller = ModelCaller(self.config, self.store)
        self.model = AsyncMock()
        self.caller.models = {role: self.model for role in ("generator", "validator")}

    async def test_completed_call_recovers_and_checks_inputs(self):
        expected = await self.call()
        self.resume()
        count = len(self.store.events)
        self.assertEqual(await self.call(), expected)
        self.model.ainvoke.assert_not_awaited()
        self.assertEqual(len(self.store.events), count)
        with self.assertRaisesRegex(ConfigurationError, "inputs differ"):
            await self.caller.call(payload={**self.payload, "source": "changed"}, **self.arguments)

    async def test_raw_response_survives_interruption_before_success(self):
        expected = await self.call()
        events = [event for event in self.store.events if event["event"] != "success"]
        atomic_lines(self.out / "audit.jsonl", (encode(event) for event in events))
        self.resume()
        self.assertEqual(await self.call(), expected)
        self.model.ainvoke.assert_not_awaited()
        self.assertEqual(self.store.events[-1]["event"], "success")
        self.assertEqual(self.store.events[-1]["technical_attempt"], 1)

    async def test_invalid_json_retries_within_the_same_call(self):
        self.model.ainvoke.side_effect = [
            AIMessage(content="invalid"),
            AIMessage(content='{"prompt":"A cat."}'),
        ]
        value = await self.call()
        self.assertEqual(value.prompt, "A cat.")
        self.assertEqual(self.model.ainvoke.await_count, 2)
        self.assertEqual(
            [
                event["technical_attempt"]
                for event in self.store.events
                if event["event"] == "request"
            ],
            [1, 2],
        )

    async def test_native_output_uses_the_same_contract(self):
        self.config.generator.structured_output = "native"
        native = AsyncMock()
        native.ainvoke.return_value = {
            "raw": AIMessage(content='{"prompt":"A cat."}'),
            "parsed": EnglishDraft(prompt="A cat."),
        }
        self.model.with_structured_output = Mock(return_value=native)
        value = await self.call()
        self.assertEqual(value.prompt, "A cat.")
        self.model.with_structured_output.assert_called_once_with(
            EnglishDraft, method="json_schema", include_raw=True, strict=False
        )
        self.assertEqual(self.store.events[-1]["data"]["parsed"], {"prompt": "A cat."})

    async def test_exhausted_technical_budget_is_preserved_on_resume(self):
        self.model.ainvoke.return_value = AIMessage(content="invalid")
        with self.assertRaises(TechnicalFailure):
            await self.call()
        self.assertEqual(self.model.ainvoke.await_count, 5)
        self.resume()
        count = len(self.store.events)
        with self.assertRaisesRegex(TechnicalFailure, "budget exhausted"):
            await self.call()
        self.model.ainvoke.assert_not_awaited()
        self.assertEqual(len(self.store.events), count)

    async def test_unknown_outcomes_still_consume_the_budget(self):
        self.model.ainvoke.side_effect = asyncio.CancelledError()
        for _ in range(5):
            with self.assertRaises(asyncio.CancelledError):
                await self.call()
        self.assertTrue(all(event["event"] == "request" for event in self.store.events))
        self.resume()
        with self.assertRaisesRegex(TechnicalFailure, "budget exhausted"):
            await self.call()
        self.model.ainvoke.assert_not_awaited()

    async def test_cached_success_still_checks_the_contract(self):
        await self.call()
        count = len(self.store.events)

        def reject(value):
            raise ValueError("contract check failed")

        with self.assertRaisesRegex(ValueError, "contract check failed"):
            await self.call(check=reject)
        self.assertEqual(len(self.store.events), count)
        self.assertEqual(self.model.ainvoke.await_count, 1)

    async def test_authentication_error_stops_and_redacts_the_run_key(self):
        response = httpx.Response(401, request=httpx.Request("POST", "http://127.0.0.1:1/v1"))
        self.model.ainvoke.side_effect = openai.AuthenticationError(
            "invalid test-secret", response=response, body=None
        )
        with self.assertRaises(ConfigurationError) as error:
            await self.call()
        self.assertNotIn("test-secret", str(error.exception))
        self.assertNotIn("test-secret", (self.out / "audit.jsonl").read_text())
        self.assertEqual(self.model.ainvoke.await_count, 1)
        self.assertEqual(self.store.events[-1]["event"], "fatal")

    async def test_default_thinking_switch_reaches_both_model_roles(self):
        settings = ModelConfig(model="deepseek-flash", base_url="https://api.deepseek.com")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            caller = ModelCaller(Config(generator=settings, validator=settings), self.store)
        for role in ("generator", "validator"):
            payload = caller.models[role]._get_request_payload([])
            self.assertEqual(payload["extra_body"]["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", payload)
