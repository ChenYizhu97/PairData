"""运行生命周期：规划任务 → 并发执行工作流 → 提交 → 导出与预览。

Run lifecycle: plan tasks → execute workflows concurrently → commit → export and preview.
"""

import asyncio
from uuid import uuid4

from midasdata.config import ConfigurationError, RunSettings
from midasdata.execution import ExecutionContext
from midasdata.exports import export_run
from midasdata.model import LLMModel
from midasdata.planning import expansion_tasks, source_tasks
from midasdata.preview import write_preview
from midasdata.sampler import load_sampler
from midasdata.schemas import TaskResult
from midasdata.storage import RunStore, file_digest
from midasdata.workflows.prompt_expansion import build_prompt_expansion
from midasdata.workflows.source_generation import build_source_generation


async def enqueue_tasks(queue: asyncio.Queue, store: RunStore, worker_count: int):
    for task in store.tasks():
        if not store.completed(task["id"]):
            await queue.put(task)
    for _ in range(worker_count):
        await queue.put(None)


async def execute_tasks(queue: asyncio.Queue, store: RunStore, workflow):
    command = store.manifest["workflow"]
    while True:
        task = await queue.get()
        try:
            if task is None:
                return
            inputs = {"task_id": task["id"]}
            if command == "generate":
                inputs["conditions"] = task["sample"]
            else:
                inputs["source_prompt"] = task["prompt"]
            completed = await workflow.ainvoke(inputs, config={"recursion_limit": 100})
            result = TaskResult.model_validate(completed["result"])
            result.source = {
                **task,
                "origin": "llm" if command == "generate" else store.manifest["request"]["input"],
            }
            store.commit(result)
        finally:
            queue.task_done()


async def process_run(store: RunStore, settings: RunSettings, sampler: dict | None):
    models = {}
    for role in ("generator", "validator"):
        model_settings = getattr(settings, role)
        api_key = settings.api_key(model_settings)
        assert api_key is not None, "model credentials must be checked before execution"
        models[role] = LLMModel(model_settings, api_key=api_key, timeout=settings.timeout)
    execution = ExecutionContext(
        store, asyncio.Semaphore(settings.concurrency), settings.retry_initial, settings.retry_max
    )
    workflow = (
        build_source_generation(models["generator"], models["validator"], execution, sampler)
        if store.manifest["workflow"] == "generate"
        else build_prompt_expansion(
            models["generator"], models["validator"], execution, settings.piu
        )
    )
    queue = asyncio.Queue(maxsize=settings.concurrency * 2)
    jobs = [asyncio.create_task(enqueue_tasks(queue, store, settings.concurrency))]
    jobs.extend(
        asyncio.create_task(execute_tasks(queue, store, workflow))
        for _ in range(settings.concurrency)
    )
    try:
        await asyncio.gather(*jobs)
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


async def run_command(args, settings: RunSettings) -> dict:
    settings.require_credentials()
    run_id = uuid4().hex[:12]
    sampler = load_sampler(args.sampler) if args.command == "generate" else None
    if args.command == "generate":
        if args.count < 0:
            raise ValueError("--count must be nonnegative")
        request = {"count": args.count, "seed": args.seed}
        tasks = source_tasks(args.count, args.seed, sampler, run_id)
    else:
        request = {"input": str(args.input.resolve()), "input_digest": file_digest(args.input)}
        tasks = expansion_tasks(args.input)
    store = RunStore(args.out)
    try:
        store.open(
            args.command,
            settings,
            request,
            sampler,
            resume=args.resume,
            run_id=run_id,
            tasks=tasks,
        )
        try:
            await process_run(store, settings, sampler)
        except BaseException as error:
            status = "stopped" if isinstance(error, ConfigurationError) else "interrupted"
            store.manifest["error"] = (
                str(error) if isinstance(error, ConfigurationError) else type(error).__name__
            )
            export_run(store, status)
            write_preview(store, settings.preview_page_size)
            raise
        statistics = export_run(store, "exporting")
        write_preview(store, settings.preview_page_size)
        store.set_status("complete")
        return statistics
    finally:
        store.close()
