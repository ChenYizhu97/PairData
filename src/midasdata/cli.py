"""MidasData generate/expand commands; workers share the model request limit."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from midasdata.config import ConfigurationError, check_credentials, load_config
from midasdata.io import RunStore, file_digest
from midasdata.llm import ModelCaller
from midasdata.preview import write_preview
from midasdata.sampler import load_sampler
from midasdata.schemas import SampleResult
from midasdata.workflows.expand import build_expand
from midasdata.workflows.generate import build_generate


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="midasdata",
        description="MidasData：以中文为主的视觉描述生成与三档英文扩写 SFT 数据工具",
    )
    commands = root.add_subparsers(dest="command", required=True)
    for name, description in (
        ("generate", "生成以中文为主的视觉描述，支持混入英文关键词和纯英文"),
        ("expand", "独立生成三档英文扩写"),
    ):
        sub = commands.add_parser(name, help=description)
        sub.add_argument("--config", type=Path, help="TOML 配置；默认从 .env 获取模型信息")
        sub.add_argument("--env-file", type=Path, default=Path(".env"))
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument("--resume", action="store_true")
        sub.add_argument("--model", help="覆盖生成模型")
        sub.add_argument("--validator-model", help="覆盖校验模型")
        sub.add_argument("--base-url")
        sub.add_argument("--structured-output", choices=("native", "json"))
        sub.add_argument("--concurrency", type=int)
        sub.add_argument("--timeout", type=float)
        sub.add_argument("--preview-page-size", type=int)
        if name == "generate":
            sub.add_argument(
                "--count", type=int, required=True, help="计划任务数，不自动补齐拒绝样本"
            )
            sub.add_argument("--seed", type=int, default=42)
            sub.add_argument("--sampler", type=Path, help="自定义采样选项文件")
        else:
            sub.add_argument(
                "--input", type=Path, required=True, help="含 id、prompt 的 UTF-8 JSONL"
            )
    return root


async def process(store, config, sampler):
    caller = ModelCaller(config, store)
    graph = (
        build_generate(caller, sampler)
        if store.manifest["workflow"] == "generate"
        else build_expand(caller)
    )
    queue = asyncio.Queue(maxsize=config.concurrency * 2)

    async def producer():
        for task in store.tasks():
            if not store.completed(task["id"]):
                await queue.put(task)
        for _ in range(config.concurrency):
            await queue.put(None)

    async def worker():
        while True:
            task = await queue.get()
            try:
                if task is None:
                    return
                inputs = (
                    {"id": task["id"], "sample": task["sample"]}
                    if store.manifest["workflow"] == "generate"
                    else {"id": task["id"], "prompt": task["prompt"]}
                )
                state = await graph.ainvoke(inputs, config={"recursion_limit": 100})
                result = SampleResult.model_validate(state["result"])
                result.source = {
                    **task,
                    "origin": "llm"
                    if result.workflow == "generate"
                    else store.manifest["request"]["input"],
                }
                store.commit(result)
            finally:
                queue.task_done()

    jobs = [asyncio.create_task(producer())] + [
        asyncio.create_task(worker()) for _ in range(config.concurrency)
    ]
    try:
        await asyncio.gather(*jobs)
    except BaseException:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise


async def execute(args, config):
    sampler = load_sampler(args.sampler) if args.command == "generate" else None
    if args.command == "generate":
        if args.count < 0:
            raise ValueError("--count must be nonnegative")
        request = {"count": args.count, "seed": args.seed}
    else:
        request = {"input": str(args.input.resolve()), "input_digest": file_digest(args.input)}
    store = RunStore(args.out)
    try:
        store.prepare(args.command, config, request, sampler, args.resume)
        try:
            await process(store, config, sampler)
        except BaseException as exc:
            status = "stopped" if isinstance(exc, ConfigurationError) else "interrupted"
            store.manifest["error"] = (
                str(exc) if isinstance(exc, ConfigurationError) else type(exc).__name__
            )
            store.export(status)
            write_preview(store, config.preview_page_size)
            raise
        stats = store.export("exporting")
        write_preview(store, config.preview_page_size)
        store.set_status("complete")
        return stats
    finally:
        store.close()


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    overrides = {
        key: getattr(args, key)
        for key in (
            "model",
            "validator_model",
            "base_url",
            "structured_output",
            "concurrency",
            "timeout",
            "preview_page_size",
        )
    }
    try:
        config = load_config(args.config, overrides, args.env_file)
        check_credentials(config)
        stats = asyncio.run(execute(args, config))
        print(json.dumps({"out": str(args.out), **stats}, ensure_ascii=False))
        return 0
    except ValidationError as exc:
        errors = "; ".join(
            ".".join(map(str, item["loc"])) + ": " + item["msg"]
            for item in exc.errors(include_input=False)
        )
        print(f"配置或数据错误：{errors}", file=sys.stderr)
        return 2
    except (ValueError, OSError, ConfigurationError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断；使用相同参数和 --resume 恢复。", file=sys.stderr)
        return 130
