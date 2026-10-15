"""Validate recipes or run the fixed synthesis pipeline."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from pairdata.config import ConfigurationError
from pairdata.recipe import load_recipe
from pairdata.runner import run_command


def create_parser():
    parser = argparse.ArgumentParser(
        prog="pairdata", description="Configurable source-target dataset synthesis"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "run"):
        sub = commands.add_parser(
            name, help="Validate a recipe" if name == "check" else "Synthesize source-target pairs"
        )
        sub.add_argument(
            "--recipe",
            type=Path,
            default=Path("recipes/rewrite/recipe.yaml"),
            help="Recipe YAML (default: recipes/rewrite/recipe.yaml, relative to working directory)",
        )
        sub.add_argument("--env-file", type=Path, default=Path(".env"))
        if name == "run":
            sub.add_argument("--count", type=int, required=True)
            sub.add_argument("--seed", type=int, default=42)
            sub.add_argument("--out", type=Path, required=True)
            sub.add_argument("--resume", action="store_true")
            sub.add_argument("--concurrency", type=int)
            sub.add_argument("--timeout", type=float)
            sub.add_argument("--preview-page-size", type=int)
    return parser


def main(argv=None):
    args = create_parser().parse_args(argv)
    try:
        overrides = {
            key: getattr(args, key, None) for key in ("concurrency", "timeout", "preview_page_size")
        }
        recipe = load_recipe(args.recipe, overrides, args.env_file)
        if args.command == "check":
            result = {"recipe": recipe.settings.name, "valid": True}
        else:
            result = {"out": str(args.out), **asyncio.run(run_command(args, recipe))}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ValidationError as error:
        messages = [
            ".".join(map(str, item["loc"])) + ": " + item["msg"]
            for item in error.errors(include_input=False)
        ]
        print("Configuration/data error: " + "; ".join(messages), file=sys.stderr)
        return 2
    except (ValueError, OSError, ConfigurationError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; repeat the command with --resume to continue.", file=sys.stderr)
        return 130
