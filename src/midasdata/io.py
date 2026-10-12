"""One synchronous writer in the event loop; durable calls and atomic sample commits."""

import fcntl
import hashlib
import json
import os
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from pydantic import BaseModel

from midasdata import __version__, prompts
from midasdata.config import Config
from midasdata.prompts import SFT_VERSION, VERSIONS, sft_record
from midasdata.sampler import DIMENSIONS, sample_conditions
from midasdata.schemas import (
    AuditEvent,
    ExpandedRecord,
    GeneratedRecord,
    SampleResult,
    SourceRecord,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_writer(path: Path) -> Iterator[TextIO]:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_lines(path: Path, lines: Iterable[str]) -> None:
    with atomic_writer(path) as handle:
        for line in lines:
            handle.write(line + "\n")


def atomic_json(path: Path, value) -> None:
    atomic_lines(path, [encode(value)])


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: invalid JSON") from exc


def input_records(path: Path):
    seen = set()
    for number, row in enumerate(read_jsonl(path), 1):
        try:
            record = (
                GeneratedRecord.model_validate(row)
                if "sample" in row
                else SourceRecord.model_validate(row)
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"input record {number}: expected id, prompt, optional sample"
            ) from exc
        if record.id in seen:
            raise ValueError(f"duplicate input ID: {record.id}")
        seen.add(record.id)
        yield record.model_dump(exclude_none=True)


def fingerprint(config: Config, request: dict, sampler: dict | None) -> str:
    values = config.model_dump(exclude={"concurrency", "preview_page_size"})
    return digest(
        {
            "config": values,
            "request": request,
            "sampler": sampler,
            "prompts": VERSIONS,
            "prompt_digest": file_digest(Path(prompts.__file__)),
            "sft": SFT_VERSION,
            "version": __version__,
        }
    )


def business_records(result: SampleResult) -> list[GeneratedRecord | ExpandedRecord]:
    return result.records if result.status == "passed" else []


def lineage_record(result: SampleResult) -> dict:
    return {
        "id": result.id,
        "source": result.source,
        "concept": result.concept,
        "status": result.status,
    }


def training_records(result: SampleResult) -> list[dict]:
    return [
        sft_record(result.source["prompt"], record.level, record.prompt)
        for record in business_records(result)
        if isinstance(record, ExpandedRecord)
    ]


def write_records(handle: TextIO, records: Iterable[dict | BaseModel]) -> None:
    for record in records:
        handle.write(encode(record) + "\n")


class RunStore:
    def __init__(self, out: Path):
        self.out = out
        self.manifest = {}
        self.events: list[dict] = []
        self.calls: dict[str, list[dict]] = defaultdict(list)
        self.lock = None

    def prepare(
        self, workflow: str, config: Config, request: dict, sampler: dict | None, resume: bool
    ):
        if self.lock is not None:
            raise ValueError("run store is already open")
        self.manifest = {}
        self.events = []
        self.calls = defaultdict(list)
        expected = fingerprint(config, request, sampler)
        if not resume:
            self.out.mkdir(parents=True, exist_ok=False)
        elif not self.out.is_dir():
            raise ValueError("--resume requires an existing run directory")
        self.lock = (self.out / "run.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.close()
            raise ValueError("this run directory is already in use") from exc
        if resume:
            self.manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
            if self.manifest["fingerprint"] != expected or self.manifest["workflow"] != workflow:
                raise ValueError("resume input/config/prompt fingerprint mismatch")
            if file_digest(self.out / "tasks.jsonl") != self.manifest["tasks_digest"]:
                raise ValueError("saved task sequence is corrupted")
            self._restore_audit()
            self.set_status("running", error="")
            return
        (self.out / "results").mkdir()
        run_id = uuid4().hex[:12]
        if workflow == "generate":
            rng = random.Random(request["seed"])
            rows = (
                {"id": f"gen:{run_id}:{index + 1:06d}", "sample": sample_conditions(rng, sampler)}
                for index in range(request["count"])
            )
        else:
            rows = input_records(Path(request["input"]))
        # Validation finishes before any model requests are made.
        atomic_lines(self.out / "tasks.jsonl", (encode(row) for row in rows))
        count = sum(1 for _ in self.tasks())
        self.manifest = {
            "run_id": run_id,
            "workflow": workflow,
            "status": "running",
            "created_at": now(),
            "fingerprint": expected,
            "config": config.model_dump(),
            "request": request,
            "tasks_digest": file_digest(self.out / "tasks.jsonl"),
            "planned": count,
            "prompt_versions": VERSIONS,
            "prompt_digest": file_digest(Path(prompts.__file__)),
            "sft_version": SFT_VERSION,
            "version": __version__,
        }
        atomic_json(self.out / "manifest.json", self.manifest)
        source = {
            "type": "llm" if workflow == "generate" else "jsonl",
            "input": request,
            "sampler": sampler,
            "version": __version__,
        }
        atomic_json(self.out / "sources.json", source)

    def _restore_audit(self):
        path = self.out / "audit.jsonl"
        if not path.exists():
            return
        with path.open("rb+") as handle:
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # Only a torn final append may be discarded. Requests are fsynced before send.
                    handle.truncate(start)
                    handle.flush()
                    os.fsync(handle.fileno())
                    break
                try:
                    event = AuditEvent.model_validate_json(line).model_dump()
                except ValueError as exc:
                    raise ValueError(
                        "audit log is corrupted; refusing to reset request budgets"
                    ) from exc
                self.events.append(event)
                self.calls[event["logical_call_id"]].append(event)

    def tasks(self):
        yield from read_jsonl(self.out / "tasks.jsonl")

    def result_path(self, sample_id: str) -> Path:
        return self.out / "results" / (hashlib.sha256(sample_id.encode()).hexdigest() + ".json")

    def completed(self, sample_id: str) -> bool:
        return self.result_path(sample_id).exists()

    def commit(self, result: SampleResult):
        atomic_json(self.result_path(result.id), result.model_dump())

    def results(self) -> Iterator[SampleResult]:
        for task in self.tasks():
            path = self.result_path(task["id"])
            if path.exists():
                yield SampleResult.model_validate_json(path.read_text(encoding="utf-8"))

    def append_event(self, **fields):
        event = AuditEvent(run_id=self.manifest["run_id"], timestamp=now(), **fields).model_dump()
        with (self.out / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(encode(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.events.append(event)
        self.calls[event["logical_call_id"]].append(event)

    def set_status(self, status: str, error: str | None = None):
        self.manifest.update(status=status, updated_at=now())
        if error is not None:
            self.manifest["error"] = error
        atomic_json(self.out / "manifest.json", self.manifest)

    def export(self, status: str) -> dict:
        filenames = ["data.jsonl", "rejected.jsonl", "lineage.jsonl"]
        include_sft = self.manifest["workflow"] == "expand"
        if include_sft:
            filenames.append("sft.jsonl")
        # Stream each committed result once into temporary files. Each file is
        # fsynced and atomically replaced only after all rows have been converted.
        with ExitStack() as stack:
            writers = {
                name: stack.enter_context(atomic_writer(self.out / name)) for name in filenames
            }
            for result in self.results():
                write_records(writers["data.jsonl"], business_records(result))
                if result.status == "rejected":
                    write_records(writers["rejected.jsonl"], [result])
                write_records(writers["lineage.jsonl"], [lineage_record(result)])
                if include_sft:
                    write_records(writers["sft.jsonl"], training_records(result))
        stats = self.statistics()
        self.manifest.update(statistics=stats)
        self.set_status(status)
        return stats

    def statistics(self) -> dict:
        states, reasons = Counter(), Counter()
        planned, passed = (
            {dim: Counter() for dim in DIMENSIONS},
            {dim: Counter() for dim in DIMENSIONS},
        )
        for row in self.tasks():
            for dim in DIMENSIONS:
                planned[dim][row.get("sample", {}).get(dim, "(dropout)")] += 1
        for row in self.results():
            states[row.status] += 1
            if row.status == "rejected":
                reasons[row.reason] += 1
            else:
                for dim in DIMENSIONS:
                    passed[dim][row.source.get("sample", {}).get(dim, "(dropout)")] += 1
        requests = [e for e in self.events if e["event"] == "request"]
        usage = Counter()
        for event in self.events:
            if event["event"] == "response":
                usage.update(
                    {
                        k: v
                        for k, v in event["data"].get("usage", {}).items()
                        if isinstance(v, int) and not isinstance(v, bool)
                    }
                )
        logical_generations = {
            e["logical_call_id"] for e in requests if e["stage"] in {"generate", "expand"}
        }
        quality_retries = {
            e["logical_call_id"]
            for e in requests
            if e["stage"] in {"generate", "expand"} and e["generation_attempt"] > 1
        }
        return {
            "planned": self.manifest["planned"],
            "passed": states["passed"],
            "rejected": states["rejected"],
            "pending": self.manifest["planned"] - sum(states.values()),
            "rejection_reasons": dict(reasons),
            "requests": len(requests),
            "technical_retries": sum(e["technical_attempt"] > 1 for e in requests),
            "logical_generations": len(logical_generations),
            "quality_retries": len(quality_retries),
            "token_usage": dict(usage),
            "sampled_distribution": planned,
            "passed_distribution": passed,
        }

    def close(self):
        if self.lock:
            self.lock.close()
            self.lock = None
