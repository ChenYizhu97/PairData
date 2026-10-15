"""Execute model operations with replay, shared concurrency, retries and audit events."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pairdata.config import ConfigurationError
from pairdata.storage import RunStore, content_digest


@dataclass
class ExecutionContext:
    """Execution resources shared by all tasks in a run."""

    store: RunStore
    slots: asyncio.Semaphore
    retry_initial: float = 1.0
    retry_max: float = 30.0


class ExecutionFailed(Exception):
    """The operation exhausted its technical attempt budget."""


def operation_metadata(
    task_id: str,
    stage: str,
    *,
    model: str = "",
    generation_attempt: int = 0,
) -> dict:
    return {
        "sample_id": task_id,
        "stage": stage,
        "generation_attempt": generation_attempt,
        "logical_call_id": content_digest([task_id, stage, generation_attempt]),
        "model": model,
    }


def _restore_operation(
    store: RunStore,
    history: list[dict],
    metadata: dict,
    payload_hash: str,
    parse: Callable[[dict], dict],
    retry_if: Callable[[Exception], bool],
) -> tuple[dict | None, int]:
    """Restore a validated response and the number of attempts already used."""
    attempts_used = 0
    for event in history:
        if event["event"] == "request":
            if event["data"].get("payload_hash") != payload_hash:
                raise ConfigurationError("replayed operation inputs differ from saved inputs")
            attempts_used = max(attempts_used, event["technical_attempt"])
    for event in history:
        if event["event"] == "success":
            return parse(event["data"]), attempts_used
    # If interrupted after saving output, validate it and append the missing success event.
    for event in reversed(history):
        if event["event"] != "response":
            continue
        try:
            result = parse(event["data"])
        except Exception as error:
            if not retry_if(error):
                raise
            break
        store.append_event(
            **metadata,
            event="success",
            technical_attempt=event["technical_attempt"],
            data=event["data"],
        )
        return result, attempts_used
    return None, attempts_used


async def execute(
    operation: Callable[[], Awaitable[dict]],
    *,
    context: ExecutionContext,
    metadata: dict,
    payload: dict,
    parse: Callable[[dict], dict],
    retry_if: Callable[[Exception], bool],
    max_attempts: int,
    describe_error: Callable[[Exception], str] = str,
) -> dict:
    """Persist each response, then parse it and apply the caller's retry policy."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    store = context.store
    payload_hash = content_digest(payload)
    history = store.operation_events.get(metadata["logical_call_id"], [])
    saved_output, attempts_used = _restore_operation(
        store, history, metadata, payload_hash, parse, retry_if
    )
    if saved_output is not None:
        return saved_output
    if attempts_used >= max_attempts:
        raise ExecutionFailed(f"{metadata['stage']}: technical attempt budget exhausted")

    for attempt in range(attempts_used + 1, max_attempts + 1):
        fields = {**metadata, "technical_attempt": attempt}
        async with context.slots:
            # Persist the attempt before execution; cancellations and unknown outcomes consume it.
            store.append_event(
                **fields,
                event="request",
                data={"payload_hash": payload_hash, "input": payload},
            )
            try:
                output = await operation()
                store.append_event(**fields, event="response", data=output)
                result = parse(output)
            except Exception as error:
                reason = describe_error(error)
                retryable = retry_if(error)
                store.append_event(
                    **fields,
                    event="technical_failure" if retryable else "fatal",
                    data={"reason": reason},
                )
                if not retryable:
                    raise
            else:
                store.append_event(**fields, event="success", data=output)
                return result
        if attempt == max_attempts:
            raise ExecutionFailed(f"{metadata['stage']}: {reason}") from None
        delay = min(
            context.retry_max,
            context.retry_initial * 2 ** (attempt - 1) + random.uniform(0, context.retry_initial),
        )
        # Backoff waits do not occupy concurrency slots.
        await asyncio.sleep(delay)
