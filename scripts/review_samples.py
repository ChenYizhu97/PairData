"""从已提交任务生成供人工填写的评审 CSV。

Generate a review CSV from committed tasks for manual assessment.
"""

import argparse
import csv
import json
from pathlib import Path

from midasdata.schemas import PromptExpansionDetails, SourceGenerationDetails
from midasdata.storage import RunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "workflow",
        "automatic_status",
        "source",
        "sample",
        "light",
        "medium",
        "rich",
        "automatic_reason",
        "human_semantics",
        "human_piu",
        "human_level_order",
        "false_accept_or_reject",
        "reviewer",
        "notes",
    ]
    store = RunStore(args.run)
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        for result in store.results():
            prompts = {}
            source_prompt = result.source.get("prompt", "")
            if isinstance(result.details, PromptExpansionDetails):
                prompts = {
                    level: expansion.prompt for level, expansion in result.details.levels.items()
                }
            elif isinstance(result.details, SourceGenerationDetails):
                source_prompt = result.details.prompt
            writer.writerow(
                {
                    "id": result.id,
                    "workflow": result.workflow,
                    "automatic_status": result.status,
                    "source": source_prompt,
                    "sample": json.dumps(result.source.get("sample", {}), ensure_ascii=False),
                    **prompts,
                    "automatic_reason": result.reason,
                }
            )


if __name__ == "__main__":
    main()
