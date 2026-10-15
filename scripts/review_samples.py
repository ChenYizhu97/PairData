"""Export completed source-target tasks to a CSV for manual review."""

import argparse
import csv
import json
from pathlib import Path

from pairdata.storage import RunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "status",
        "source",
        "target",
        "source_sample",
        "target_sample",
        "reason",
        "human_judgment",
        "reviewer",
        "notes",
    ]
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        for result in RunStore(args.run).results():
            writer.writerow(
                {
                    "id": result.id,
                    "status": result.status,
                    "reason": result.reason,
                    "source": json.dumps(result.source.candidate, ensure_ascii=False),
                    "target": json.dumps(result.target.candidate, ensure_ascii=False)
                    if result.target
                    else "",
                    "source_sample": json.dumps(result.source.sample, ensure_ascii=False),
                    "target_sample": json.dumps(result.target.sample, ensure_ascii=False)
                    if result.target
                    else "",
                }
            )


if __name__ == "__main__":
    main()
