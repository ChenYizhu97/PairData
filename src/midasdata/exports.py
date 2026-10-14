"""从已提交任务派生业务 JSONL、SFT、来源记录和统计。

Derive business JSONL, SFT data, lineage and statistics from committed tasks.
"""

from collections import Counter
from contextlib import ExitStack

from midasdata.prompts import sft_record
from midasdata.sampler import DIMENSIONS
from midasdata.schemas import ExpandedPromptRecord, TaskResult
from midasdata.storage import RunStore, atomic_writer, encode_json


def training_records(result: TaskResult) -> list[dict]:
    if result.status != "passed":
        return []
    return [
        sft_record(result.source["prompt"], record.level, record.prompt)
        for record in result.records
        if isinstance(record, ExpandedPromptRecord)
    ]


def run_statistics(store: RunStore) -> dict:
    statuses, reasons, usage = Counter(), Counter(), Counter()
    planned = {dimension: Counter() for dimension in DIMENSIONS}
    passed = {dimension: Counter() for dimension in DIMENSIONS}
    for task in store.tasks():
        for dimension in DIMENSIONS:
            planned[dimension][task.get("sample", {}).get(dimension, "(dropout)")] += 1
    for result in store.results():
        statuses[result.status] += 1
        if result.status == "rejected":
            reasons[result.reason] += 1
        else:
            for dimension in DIMENSIONS:
                passed[dimension][result.source.get("sample", {}).get(dimension, "(dropout)")] += 1
    requests = [event for event in store.events if event["event"] == "request"]
    for event in store.events:
        if event["event"] == "response":
            usage.update(
                {
                    key: value
                    for key, value in event["data"].get("usage", {}).items()
                    if isinstance(value, int) and not isinstance(value, bool)
                }
            )
    generations = [event for event in requests if event["stage"] in {"generate", "expand"}]
    return {
        "planned": store.manifest["planned"],
        "passed": statuses["passed"],
        "rejected": statuses["rejected"],
        "pending": store.manifest["planned"] - sum(statuses.values()),
        "rejection_reasons": dict(reasons),
        "requests": len(requests),
        "technical_retries": sum(event["technical_attempt"] > 1 for event in requests),
        "logical_generations": len({event["logical_call_id"] for event in generations}),
        "quality_retries": len(
            {event["logical_call_id"] for event in generations if event["generation_attempt"] > 1}
        ),
        "token_usage": dict(usage),
        "sampled_distribution": planned,
        "passed_distribution": passed,
    }


def export_run(store: RunStore, status: str) -> dict:
    filenames = ["data.jsonl", "lineage.jsonl", "rejected.jsonl"]
    if store.manifest["workflow"] == "expand":
        filenames.append("sft.jsonl")
    with ExitStack() as stack:
        writers = {
            name: stack.enter_context(atomic_writer(store.directory / name)) for name in filenames
        }
        for result in store.results():
            records = result.records if result.status == "passed" else []
            for record in records:
                writers["data.jsonl"].write(encode_json(record) + "\n")
            lineage = {
                "id": result.id,
                "source": result.source,
                "concept": result.concept,
                "status": result.status,
            }
            writers["lineage.jsonl"].write(encode_json(lineage) + "\n")
            if result.status == "rejected":
                writers["rejected.jsonl"].write(encode_json(result) + "\n")
            if "sft.jsonl" in writers:
                for record in training_records(result):
                    writers["sft.jsonl"].write(encode_json(record) + "\n")
    statistics = run_statistics(store)
    store.manifest["statistics"] = statistics
    store.set_status(status)
    return statistics
