"""Execute model operations with replay, shared concurrency, retries and audit events."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from pairdata.config import ConfigurationError
from pairdata.schemas import (
    ModelResponse,
    OperationEvent,
    OperationMetadata,
    OperationName,
    StageName,
)
from pairdata.storage import RunStore, content_digest
from pairdata.validation import ResponseValidationError


@dataclass
class ExecutionContext:
    """Execution resources shared by all tasks in a run."""

    store: RunStore
    slots: asyncio.Semaphore
    retry_initial: float = 1.0
    retry_max: float = 30.0
    seed: int = 42


class ExecutionFailed(Exception):
    """The operation exhausted its technical attempt budget."""


def operation_metadata(
    task_id: str,
    stage: StageName,
    operation: OperationName,
    *,
    model: str = "",
    generation_attempt: int = 0,
) -> OperationMetadata:
    operation_path = f"{stage}.{operation}"
    return OperationMetadata(
        task_id=task_id,
        operation=operation_path,
        logical_call_id=content_digest([task_id, operation_path, generation_attempt]),
        generation_attempt=generation_attempt,
        model=model,
    )


def _restore_operation(
    store: RunStore,
    history: tuple[OperationEvent, ...],
    metadata: OperationMetadata,
    payload_hash: str,
    parse: Callable[[ModelResponse], dict],
) -> tuple[dict | None, int]:
    """Restore a validated response and the number of attempts already used."""
    attempts_used = 0
    for event in history:
        if event.event == "request":
            if event.data.get("payload_hash") != payload_hash:
                raise ConfigurationError("replayed operation inputs differ from saved inputs")
            attempts_used = max(attempts_used, event.request_attempt)
    for event in history:
        if event.event == "success":
            return parse(event.data), attempts_used
    # If interrupted after saving output, validate it and append the missing success event.
    for event in reversed(history):
        if event.event != "response":
            continue
        try:
            result = parse(event.data)
        except ResponseValidationError:
            break
        store.append_event(
            **asdict(metadata),
            event="success",
            request_attempt=event.request_attempt,
            data=event.data,
        )
        return result, attempts_used
    return None, attempts_used


async def execute(
    operation: Callable[[], Awaitable[ModelResponse]],
    *,
    context: ExecutionContext,
    metadata: OperationMetadata,
    payload: dict,
    parse: Callable[[ModelResponse], dict],
    retry_if: Callable[[Exception], bool],
    max_attempts: int,
    describe_error: Callable[[Exception], str] = str,
) -> dict:
    """Persist each response, then parse it and apply the caller's retry policy."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    store = context.store
    payload_hash = content_digest(payload)
    history = store.operation_history(metadata.logical_call_id)
    saved_output, attempts_used = _restore_operation(store, history, metadata, payload_hash, parse)
    if saved_output is not None:
        return saved_output
    if attempts_used >= max_attempts:
        raise ExecutionFailed(f"{metadata.operation}: technical attempt budget exhausted")

    for attempt in range(attempts_used + 1, max_attempts + 1):
        fields = {**asdict(metadata), "request_attempt": attempt}
        async with context.slots:
            # Persist the attempt before execution; cancellations and unknown outcomes consume it.
            store.append_event(
                **fields,
                event="request",
                data={"payload_hash": payload_hash, "input": payload},
            )
            failure: Exception
            try:
                output = await operation()
            except Exception as error:
                failure = error
            else:
                store.append_event(**fields, event="response", data=output)
                try:
                    result = parse(output)
                except ResponseValidationError as error:
                    failure = error
                else:
                    store.append_event(**fields, event="success", data=output)
                    return result

            reason = describe_error(failure)
            retryable = isinstance(failure, ResponseValidationError) or retry_if(failure)
            store.append_event(
                **fields,
                event="technical_failure" if retryable else "fatal",
                data={"reason": reason},
            )
            if not retryable:
                raise failure
        if attempt == max_attempts:
            raise ExecutionFailed(f"{metadata.operation}: {reason}") from None
        delay = min(
            context.retry_max,
            context.retry_initial * 2 ** (attempt - 1) + random.uniform(0, context.retry_initial),
        )
        # Backoff waits do not occupy concurrency slots.
        await asyncio.sleep(delay)
