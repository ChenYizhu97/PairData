"""Derive source-target JSONL, lineage and statistics from committed tasks."""

from collections import Counter
from contextlib import ExitStack

from pairdata.storage import RunStore, atomic_writer, encode_json


def run_statistics(store: RunStore):
    results = list(store.results())
    statuses = Counter(result.status for result in results)
    requests = [event for event in store.events if event["event"] == "request"]
    usage = Counter()
    for event in store.events:
        if event["event"] == "response":
            usage.update(
                {
                    key: value
                    for key, value in event["data"].get("usage", {}).items()
                    if type(value) is int
                }
            )
    distributions = {}
    exported = {result.id for result in results if result.status == "passed"}
    for stage in ("source", "target"):
        samples = {
            event["sample_id"]: event["data"]["sample"]
            for event in store.events
            if event["event"] == "sample" and event["stage"] == f"{stage}.sample"
        }
        accepted = {
            event["sample_id"]
            for event in store.events
            if event["event"] == "quality_decision"
            and event["stage"] == f"{stage}.gate"
            and event["data"]["passed"]
        }
        fields = store.manifest["config"][stage]["sampler"]

        def distribution(ids):
            return {
                field: dict(
                    Counter(
                        encode_json(samples[key][field]) if field in samples[key] else "(dropout)"
                        for key in ids
                    )
                )
                for field in fields
            }

        distributions[stage] = {
            "sampled_tasks": len(samples),
            "not_reached": store.manifest["planned"] - len(samples),
            "sampled": distribution(samples),
            "stage_passed": distribution(accepted & samples.keys()),
            "exported": distribution(exported & samples.keys()),
        }
    generations = [event for event in requests if event["stage"].endswith(".generate")]
    return {
        "planned": store.manifest["planned"],
        "passed": statuses["passed"],
        "rejected": statuses["rejected"],
        "pending": store.manifest["planned"] - len(results),
        "rejection_reasons": dict(
            Counter(result.reason for result in results if result.status == "rejected")
        ),
        "requests": len(requests),
        "technical_retries": sum(event["technical_attempt"] > 1 for event in requests),
        "logical_generations": len({event["logical_call_id"] for event in generations}),
        "quality_retries": len(
            {event["logical_call_id"] for event in generations if event["generation_attempt"] > 1}
        ),
        "token_usage": dict(usage),
        "distributions": distributions,
    }


def export_run(store: RunStore, status: str):
    with ExitStack() as stack:
        writers = {
            name: stack.enter_context(atomic_writer(store.directory / name))
            for name in ("data.jsonl", "lineage.jsonl", "rejected.jsonl")
        }
        row_number = 0
        for result in store.results():
            if result.status == "passed":
                row_number += 1
                writers["data.jsonl"].write(
                    encode_json(
                        {"source": result.source.candidate, "target": result.target.candidate}
                    )
                    + "\n"
                )
            else:
                writers["rejected.jsonl"].write(encode_json(result) + "\n")
            writers["lineage.jsonl"].write(
                encode_json(
                    {
                        "id": result.id,
                        "status": result.status,
                        "data_row": row_number if result.status == "passed" else None,
                    }
                )
                + "\n"
            )
    statistics = run_statistics(store)
    store.manifest["statistics"] = statistics
    store.set_status(status)
    return statistics
