"""命令行只解析参数、加载配置、调用 runner 和报告结果。

The CLI parses arguments, loads configuration, invokes the runner and reports results.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from midasdata.config import ConfigurationError, load_settings
from midasdata.runner import run_command


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="midasdata",
        description="视觉源描述生成与三档英文扩写 SFT 工具",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command, description in (("generate", "生成视觉源描述"), ("expand", "生成三档英文扩写")):
        sub = commands.add_parser(command, help=description)
        sub.add_argument("--config", type=Path, help="TOML 配置")
        sub.add_argument("--env-file", type=Path, default=Path(".env"))
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument("--resume", action="store_true")
        sub.add_argument("--model", help="覆盖生成模型")
        sub.add_argument("--validator-model", help="覆盖校验模型")
        sub.add_argument("--base-url")
        sub.add_argument("--structured-output", choices=("json", "native"))
        sub.add_argument("--concurrency", type=int)
        sub.add_argument("--timeout", type=float)
        sub.add_argument("--preview-page-size", type=int)
        if command == "generate":
            sub.add_argument("--count", type=int, required=True, help="计划任务数，拒绝不补齐")
            sub.add_argument("--seed", type=int, default=42)
            sub.add_argument("--sampler", type=Path)
        else:
            sub.add_argument("--input", type=Path, required=True, help="含 id、prompt 的 JSONL")
    return parser


def main(argv=None) -> int:
    args = create_parser().parse_args(argv)
    override_names = (
        "model",
        "validator_model",
        "base_url",
        "structured_output",
        "concurrency",
        "timeout",
        "preview_page_size",
    )
    try:
        settings = load_settings(
            args.config, {key: getattr(args, key) for key in override_names}, args.env_file
        )
        statistics = asyncio.run(run_command(args, settings))
        print(json.dumps({"out": str(args.out), **statistics}, ensure_ascii=False))
        return 0
    except ValidationError as error:
        messages = [
            ".".join(map(str, item["loc"])) + ": " + item["msg"]
            for item in error.errors(include_input=False)
        ]
        print("配置或数据错误：" + "; ".join(messages), file=sys.stderr)
        return 2
    except (ValueError, OSError, ConfigurationError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断；使用相同参数和 --resume 恢复。", file=sys.stderr)
        return 130
