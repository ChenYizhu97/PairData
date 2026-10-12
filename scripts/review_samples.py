"""Create a human review sheet from real run results; never auto-approve quality."""

import argparse
import csv
import json
from pathlib import Path

from midasdata.io import RunStore
from midasdata.schemas import ExpandDetails, ExpandedRecord, GenerateDetails


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    store = RunStore(args.run)
    store.manifest = json.loads((args.run / "manifest.json").read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
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
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        for result in store.results():
            levels = {
                row.level: row.prompt for row in result.records if isinstance(row, ExpandedRecord)
            }
            if not levels and isinstance(result.details, ExpandDetails):
                levels = {level: value.prompt for level, value in result.details.levels.items()}
            prompt = result.details.prompt if isinstance(result.details, GenerateDetails) else ""
            writer.writerow(
                {
                    "id": result.id,
                    "workflow": result.workflow,
                    "automatic_status": result.status,
                    "source": result.source.get("prompt", prompt),
                    "sample": json.dumps(result.source.get("sample", {}), ensure_ascii=False),
                    **levels,
                    "automatic_reason": result.reason,
                }
            )


if __name__ == "__main__":
    main()
