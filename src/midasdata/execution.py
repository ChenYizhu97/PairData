"""单次操作的执行：历史复用、并发限制、技术重试与记录。

Execute one operation with history reuse, concurrency limits, technical retries and recording.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from midasdata.config import ConfigurationError
from midasdata.storage import RunStore, content_digest

Result = TypeVar("Result")


@dataclass
class ExecutionContext:
    """一次运行共享的执行资源；各工作流独立保存自己的业务状态。

    Execution resources shared by a run; each workflow owns its business state.
    """

    store: RunStore
    slots: asyncio.Semaphore
    retry_initial: float = 1.0
    retry_max: float = 30.0


class ExecutionFailed(Exception):
    """操作已耗尽技术尝试次数，由调用方决定如何处理。

    The operation exhausted its technical attempts; the caller decides how to proceed.
    """


def operation_metadata(
    task_id: str,
    stage: str,
    *,
    model: str = "",
    level: str | None = None,
    round_number: int = 0,
    draft_number: int = 0,
) -> dict:
    return {
        "sample_id": task_id,
        "stage": stage,
        "level": level,
        "round": round_number,
        "generation_attempt": draft_number,
        "logical_call_id": content_digest([task_id, stage, level, round_number, draft_number]),
        "model": model,
    }


def _restore_operation(
    store: RunStore,
    history: list[dict],
    metadata: dict,
    payload_hash: str,
    parse: Callable[[dict], Result],
    retry_if: Callable[[Exception], bool],
) -> tuple[dict | None, int]:
    """恢复已保存的输出和已用次数，输出仍由调用方提供的解析函数检查。

    Restore saved output and attempts used; the caller supplies the output parser.
    """
    attempts_used = 0
    for event in history:
        if event["event"] == "request":
            if event["data"].get("payload_hash") != payload_hash:
                raise ConfigurationError("replayed operation inputs differ from saved inputs")
            attempts_used = max(attempts_used, event["technical_attempt"])
    for event in history:
        if event["event"] == "success":
            return event["data"], attempts_used
    # 输出落盘后、success 追加前中断时，检查输出后补齐成功记录。
    # If interrupted after saving output, validate it and append the missing success event.
    for event in reversed(history):
        if event["event"] != "response":
            continue
        try:
            parse(event["data"])
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
        return event["data"], attempts_used
    return None, attempts_used


async def execute(
    operation: Callable[[], Awaitable[dict]],
    *,
    context: ExecutionContext,
    metadata: dict,
    payload: dict,
    parse: Callable[[dict], Result],
    retry_if: Callable[[Exception], bool],
    max_attempts: int = 1,
    describe_error: Callable[[Exception], str] = str,
) -> Result:
    """执行返回可落盘字典的操作，按调用方规则解析输出和判断能否重试。

    Execute an operation returning a persistable dict, using caller-defined parsing and retries.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    store = context.store
    payload_hash = content_digest(payload)
    history = store.operation_events.get(metadata["logical_call_id"])
    if not history:
        # 首次操作直接从第一次尝试开始。
        # A new operation starts with its first attempt.
        attempts_used = 0
    else:
        saved_output, attempts_used = _restore_operation(
            store, history, metadata, payload_hash, parse, retry_if
        )
        if saved_output is not None:
            return parse(saved_output)
    if attempts_used >= max_attempts:
        raise ExecutionFailed(f"{metadata['stage']}: technical attempt budget exhausted")

    for attempt in range(attempts_used + 1, max_attempts + 1):
        fields = {**metadata, "technical_attempt": attempt}
        async with context.slots:
            # 执行前持久化次数，取消或未知结果也消耗一次尝试。
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
        # 退避等待不占用并发槽位。
        # Backoff waits do not occupy concurrency slots.
        await asyncio.sleep(delay)
    raise RuntimeError("execution ended without a result")
