"""Run lifecycle: plan, execute tasks, commit results, export and preview."""

import asyncio
from collections.abc import Mapping
from uuid import uuid4

from pairdata.config import ConfigurationError
from pairdata.execution import ExecutionContext
from pairdata.exports import export_run
from pairdata.model import LLMModel, ModelClient
from pairdata.preview import write_preview
from pairdata.recipe import Recipe
from pairdata.storage import RunStore
from pairdata.workflows.pipeline import build_pipeline


async def enqueue_tasks(queue, store, worker_count):
    for task in store.tasks():
        if not store.completed(task["id"]):
            await queue.put(task)
    for _ in range(worker_count):
        await queue.put(None)


async def execute_tasks(queue, store, workflow, recursion_limit):
    while True:
        task = await queue.get()
        if task is None:
            return
        completed = await workflow.ainvoke(
            {"task_id": task["id"], "index": task["index"]},
            config={"recursion_limit": recursion_limit},
        )
        store.commit(completed["result"])


async def process_run(
    store: RunStore, recipe: Recipe, models: Mapping[str, ModelClient] | None = None
) -> None:
    runtime = recipe.settings.runtime
    if models is None:
        models = {
            name: LLMModel(
                settings, api_key=recipe.settings.api_key(settings), timeout=runtime.timeout
            )
            for name, settings in recipe.settings.models.items()
        }
    execution = ExecutionContext(
        store=store,
        slots=asyncio.Semaphore(runtime.concurrency),
        retry_initial=runtime.retry_initial,
        retry_max=runtime.retry_max,
        seed=store.seed,
    )
    workflow = build_pipeline(recipe, models, execution)
    # Each generation visits generate/evaluate/gate; reserve steps for initialization,
    # the two samplers and the terminal node, with a small margin.
    recursion_limit = 10 + 3 * sum(
        stage.settings.max_generations for stage in recipe.stages.values()
    )
    # Bound active tasks as well as requests. A task keeps its worker during backoff;
    # the request semaphore protects the model boundary if task scheduling changes.
    queue = asyncio.Queue(maxsize=runtime.concurrency * 2)
    jobs = [asyncio.create_task(enqueue_tasks(queue, store, runtime.concurrency))]
    jobs.extend(
        asyncio.create_task(execute_tasks(queue, store, workflow, recursion_limit))
        for _ in range(runtime.concurrency)
    )
    try:
        await asyncio.gather(*jobs)
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


async def run_command(
    args, recipe: Recipe, *, models: Mapping[str, ModelClient] | None = None
) -> dict:
    recipe.settings.require_credentials()
    if args.count < 0:
        raise ValueError("--count must be nonnegative")
    run_id = uuid4().hex[:12]
    request = {"count": args.count, "seed": args.seed}
    store = RunStore(args.out)
    try:
        store.open(
            recipe.snapshot(),
            request,
            resume=args.resume,
            run_id=run_id,
            tasks=(
                {"id": f"{run_id}:{index + 1:06d}", "index": index} for index in range(args.count)
            ),
        )
        try:
            await process_run(store, recipe, models)
        except BaseException as error:
            status = "stopped" if isinstance(error, ConfigurationError) else "interrupted"
            description = (
                str(error) if isinstance(error, ConfigurationError) else type(error).__name__
            )
            # Preserve the execution error even if writing status or derived files fails.
            # Try each independently so a failed export does not prevent a usable preview.
            recovery_actions = (
                ("save run status", lambda: store.set_status(status, error=description)),
                ("export results", lambda: export_run(store, status)),
                (
                    "write preview",
                    lambda: write_preview(store, recipe.settings.runtime.preview_page_size),
                ),
            )
            for action, recover in recovery_actions:
                try:
                    recover()
                except Exception as secondary:
                    error.add_note(f"Could not {action}: {type(secondary).__name__}")
            raise
        statistics = export_run(store, "exporting")
        write_preview(store, recipe.settings.runtime.preview_page_size)
        store.set_status("complete")
        return statistics
    finally:
        store.close()
