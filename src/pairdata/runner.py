"""Run lifecycle: plan, execute tasks, commit results, export and preview."""

import asyncio
from uuid import uuid4

from pairdata.config import ConfigurationError
from pairdata.execution import ExecutionContext
from pairdata.exports import export_run
from pairdata.model import LLMModel
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


async def process_run(store: RunStore, recipe: Recipe):
    runtime = recipe.settings.runtime
    models = {
        name: LLMModel(settings, api_key=recipe.settings.api_key(settings), timeout=runtime.timeout)
        for name, settings in recipe.settings.models.items()
    }
    execution = ExecutionContext(
        store, asyncio.Semaphore(runtime.concurrency), runtime.retry_initial, runtime.retry_max
    )
    workflow = build_pipeline(recipe, models, execution)
    recursion_limit = 10 + 3 * sum(
        stage.settings.max_generations for stage in recipe.stages.values()
    )
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


async def run_command(args, recipe: Recipe) -> dict:
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
        status = "exporting"
        try:
            await process_run(store, recipe)
        except BaseException as error:
            status = "stopped" if isinstance(error, ConfigurationError) else "interrupted"
            store.manifest["error"] = (
                str(error) if isinstance(error, ConfigurationError) else type(error).__name__
            )
            raise
        finally:
            statistics = export_run(store, status)
            write_preview(store, recipe.settings.runtime.preview_page_size)
        store.set_status("complete")
        return statistics
    finally:
        store.close()
