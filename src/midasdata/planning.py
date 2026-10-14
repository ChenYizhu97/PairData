"""在并发请求开始前确定并持久化任务序列。

Determine and persist the task sequence before concurrent requests begin.
"""

import random
from collections.abc import Iterator
from pathlib import Path

from midasdata.sampler import sample_conditions
from midasdata.schemas import InputPrompt, SourcePromptRecord
from midasdata.storage import read_jsonl


def source_tasks(count: int, seed: int, sampler: dict, run_id: str) -> Iterator[dict]:
    if count < 0:
        raise ValueError("--count must be nonnegative")
    rng = random.Random(seed)
    for index in range(count):
        yield {
            "id": f"gen:{run_id}:{index + 1:06d}",
            "sample": sample_conditions(rng, sampler),
        }


def expansion_tasks(path: Path) -> Iterator[dict]:
    seen = set()
    for line_number, row in enumerate(read_jsonl(path), 1):
        if not isinstance(row, dict):
            raise ValueError(f"input record {line_number}: expected an object")
        record_type = SourcePromptRecord if "sample" in row else InputPrompt
        record = record_type.model_validate(row)
        if record.id in seen:
            raise ValueError(f"duplicate input ID: {record.id}")
        seen.add(record.id)
        yield record.model_dump(exclude_none=True)
