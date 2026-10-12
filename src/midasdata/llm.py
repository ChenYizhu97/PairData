"""Exactly one technical retry boundary, shared semaphore, and replayable call IDs."""

import asyncio
from collections.abc import Callable, Iterator
from functools import partial
from itertools import count
from typing import TypeVar

import openai
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from midasdata.config import Config, ConfigurationError, check_credentials
from midasdata.io import RunStore, digest, encode
from midasdata.limits import MAX_TECHNICAL_ATTEMPTS
from midasdata.prompts import VERSIONS, messages
from midasdata.schemas import Decision, Level

Response = TypeVar("Response", bound=BaseModel)


def parse_response(
    data: dict, schema: type[Response], check: Callable[[Response], object] | None
) -> Response:
    value = (
        schema.model_validate(data["parsed"])
        if data.get("parsed") is not None
        else schema.model_validate_json(data["raw"])
    )
    if check:
        check(value)
    return value


class RetryableError(Exception):
    pass


class TechnicalFailure(Exception):
    pass


class ModelCaller:
    def __init__(self, config: Config, store: RunStore):
        check_credentials(config)
        self.config, self.store = config, store
        self._api_keys = {
            role: config.api_key(getattr(config, role)) for role in ("generator", "validator")
        }
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.models = {}
        for role in ("generator", "validator"):
            settings = getattr(config, role)
            self.models[role] = ChatOpenAI(
                model=settings.model,
                base_url=settings.base_url,
                api_key=self._api_keys[role],
                temperature=settings.temperature,
                reasoning_effort=settings.reasoning_effort,
                extra_body=settings.extra_body or None,
                timeout=config.timeout,
                max_retries=0,
                http_socket_options=(),
            )

    def safe_error(self, error: Exception) -> str:
        # Provider errors can echo a token; redact the actual configured values.
        chain, current = [], error
        while current is not None and len(chain) < 4:
            chain.append(f"{type(current).__name__}: {current}")
            current = current.__cause__
        value = " <- ".join(chain)
        for secret in self._api_keys.values():
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value[:2000]

    def record_decision(
        self,
        stage: str,
        decision: Decision,
        *,
        sample_id: str,
        level: Level | None = None,
        round: int = 0,
        generation_attempt: int = 0,
    ) -> None:
        logical_id = digest([sample_id, stage, level, round, generation_attempt])
        if any(event["event"] == "quality_decision" for event in self.store.calls[logical_id]):
            return
        self.store.append_event(
            sample_id=sample_id,
            stage=stage,
            level=level,
            round=round,
            generation_attempt=generation_attempt,
            logical_call_id=logical_id,
            prompt_version=VERSIONS[stage],
            event="quality_decision",
            data=decision.model_dump(),
        )

    async def call(
        self,
        stage: str,
        payload: dict,
        schema: type[Response],
        *,
        sample_id: str,
        level: Level | None = None,
        round: int = 0,
        generation_attempt: int = 0,
        check: Callable[[Response], object] | None = None,
    ) -> Response:
        logical_id = digest([sample_id, stage, level, round, generation_attempt])
        payload_hash = digest(payload)
        role = "generator" if stage in {"generate", "expand"} else "validator"
        settings = getattr(self.config, role)
        meta = dict(
            sample_id=sample_id,
            stage=stage,
            level=level,
            round=round,
            logical_call_id=logical_id,
            generation_attempt=generation_attempt,
            prompt_version=VERSIONS[stage],
            model=settings.model,
        )
        restored, attempts = self._restore_call(
            self.store.calls[logical_id], payload_hash, schema, check, meta
        )
        if restored is not None:
            return restored
        if attempts >= MAX_TECHNICAL_ATTEMPTS:
            raise TechnicalFailure(f"{stage}: technical attempt budget exhausted")

        request = partial(
            self._request_once,
            role=role,
            meta=meta,
            payload=payload,
            payload_hash=payload_hash,
            inputs=messages(stage, payload, schema),
            schema=schema,
            check=check,
            attempt_numbers=count(attempts + 1),
        )
        runnable = RunnableLambda(request).with_retry(
            retry_if_exception_type=(RetryableError,),
            stop_after_attempt=MAX_TECHNICAL_ATTEMPTS - attempts,
            exponential_jitter_params={
                "initial": self.config.retry_initial,
                "max": self.config.retry_max,
                "jitter": self.config.retry_initial,
            },
        )
        try:
            return await runnable.ainvoke(None)
        except RetryableError as exc:
            raise TechnicalFailure(f"{stage}: {exc}") from None

    def _restore_call(
        self,
        events: list[dict],
        payload_hash: str,
        schema: type[Response],
        check: Callable[[Response], object] | None,
        meta: dict,
    ) -> tuple[Response | None, int]:
        """Recover this logical call's response and attempt count within the selected run."""
        attempts = 0
        for event in events:
            if event["event"] == "request":
                if event["data"]["payload_hash"] != payload_hash:
                    raise ConfigurationError("replayed call inputs differ from saved inputs")
                attempts = max(attempts, event["technical_attempt"])
            if event["event"] == "success":
                return parse_response(event["data"], schema, check), attempts
        # A crash between response and success must not lose a valid saved response.
        for event in reversed(events):
            if event["event"] != "response":
                continue
            try:
                value = parse_response(event["data"], schema, check)
            except ValueError:
                break
            self.store.append_event(
                **meta,
                technical_attempt=event["technical_attempt"],
                event="success",
                data={"parsed": value.model_dump()},
            )
            return value, attempts
        return None, attempts

    async def _request_once(
        self,
        _,
        *,
        role: str,
        meta: dict,
        payload: dict,
        payload_hash: str,
        inputs: list[dict],
        schema: type[Response],
        check: Callable[[Response], object] | None,
        attempt_numbers: Iterator[int],
    ) -> Response:
        async with self.semaphore:
            event_fields = {**meta, "technical_attempt": next(attempt_numbers)}
            self.store.append_event(
                **event_fields,
                event="request",
                data={"payload_hash": payload_hash, "input": payload},
            )
            try:
                model = self.models[role]
                native = getattr(self.config, role).structured_output == "native"
                if native:
                    model = model.with_structured_output(
                        schema, method="json_schema", include_raw=True, strict=False
                    )
                result = await asyncio.wait_for(model.ainvoke(inputs), timeout=self.config.timeout)
                raw = result["raw"] if native else result
                parsed = result.get("parsed") if native else None
                response = {
                    "raw": raw.content if isinstance(raw.content, str) else encode(raw.content),
                    "usage": raw.usage_metadata or {},
                    "parsed": parsed.model_dump() if isinstance(parsed, BaseModel) else parsed,
                }
                self.store.append_event(**event_fields, event="response", data=response)
                value = parse_response(response, schema, check)
            except openai.APIStatusError as exc:
                error = self.safe_error(exc)
                retryable = exc.status_code in {408, 409, 429} or exc.status_code >= 500
                self.store.append_event(
                    **event_fields,
                    event="technical_failure" if retryable else "fatal",
                    data={"reason": error},
                )
                if retryable:
                    raise RetryableError(error) from None
                raise ConfigurationError(error) from None
            except (TimeoutError, openai.APIConnectionError, ValueError) as exc:
                error = self.safe_error(exc)
                self.store.append_event(
                    **event_fields, event="technical_failure", data={"reason": error}
                )
                raise RetryableError(error) from None
            self.store.append_event(
                **event_fields, event="success", data={"parsed": value.model_dump()}
            )
            return value
