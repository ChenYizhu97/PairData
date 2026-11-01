"""Run directories, atomic writes, audit logs and task commits."""

import copy
import fcntl
import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from pairdata.schemas import (
    OperationEvent,
    OperationMetadata,
    QualityDecision,
    StageName,
    TaskResult,
)


def encode_json(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump()
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def content_digest(value) -> str:
    return hashlib.sha256(encode_json(value).encode()).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_writer(path: Path) -> Generator[TextIO, None, None]:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    with atomic_writer(path) as handle:
        handle.write(encode_json(value) + "\n")


def write_jsonl(path: Path, rows: Iterable) -> None:
    with atomic_writer(path) as handle:
        for row in rows:
            handle.write(encode_json(row) + "\n")


def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from error


def run_fingerprint(snapshot: dict, request: dict) -> str:
    stable = copy.deepcopy(snapshot)
    for key in ("concurrency", "preview_page_size"):
        stable["config"]["runtime"].pop(key, None)
    return content_digest({"recipe": stable, "request": request})


class RunStore:
    """Persist a run's configuration, tasks and events for interruption recovery."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._manifest: dict = {}
        self._events: list[OperationEvent] = []
        self._operation_events: dict[str, list[OperationEvent]] = defaultdict(list)
        self._lock: TextIO | None = None

    @property
    def seed(self) -> int:
        return self._manifest["request"]["seed"]

    @property
    def planned_tasks(self) -> int:
        return self._manifest["planned"]

    @property
    def recipe_name(self) -> str:
        return self._manifest["config"]["name"]

    def sampler_fields(self, stage: StageName) -> tuple[str, ...]:
        return tuple(self._manifest["config"][stage]["sampler"])

    def iter_events(self) -> Iterator[OperationEvent]:
        """Read audit events in append order; callers must not mutate them."""
        return iter(self._events)

    def operation_history(self, logical_call_id: str) -> tuple[OperationEvent, ...]:
        return tuple(self._operation_events.get(logical_call_id, ()))

    def open(
        self,
        snapshot: dict,
        request: dict,
        *,
        resume: bool,
        run_id: str,
        tasks: Iterable[dict],
    ) -> None:
        if self._lock is not None:
            raise ValueError("run store is already open")
        expected = run_fingerprint(snapshot, request)
        if resume:
            if not self.directory.is_dir():
                raise ValueError("--resume requires an existing run directory")
        else:
            self.directory.mkdir(parents=True, exist_ok=False)
        self._lock = (self.directory / "run.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.close()
            raise ValueError("run directory is already in use") from error
        self._events = []
        self._operation_events = defaultdict(list)
        try:
            if resume:
                self._resume(expected)
            else:
                self._initialize(snapshot, request, expected, run_id, tasks)
        except BaseException:
            self.close()
            raise

    def _initialize(self, snapshot, request, fingerprint, run_id, tasks):
        (self.directory / "results").mkdir()
        write_jsonl(self.directory / "tasks.jsonl", tasks)
        write_json(self.directory / "recipe.json", snapshot)
        self._manifest = {
            "run_id": run_id,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": snapshot["config"],
            "request": request,
            "fingerprint": fingerprint,
            "tasks_digest": file_digest(self.directory / "tasks.jsonl"),
            "recipe_digest": file_digest(self.directory / "recipe.json"),
            "planned": sum(1 for _ in self.tasks()),
        }
        write_json(self.directory / "manifest.json", self._manifest)

    def _resume(self, fingerprint: str):
        self._manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        if self._manifest["fingerprint"] != fingerprint:
            raise ValueError("resume input/recipe mismatch")
        for filename, key in (("tasks.jsonl", "tasks_digest"), ("recipe.json", "recipe_digest")):
            if file_digest(self.directory / filename) != self._manifest.get(key):
                raise ValueError(f"saved {filename} is corrupted")
        saved = json.loads((self.directory / "recipe.json").read_text(encoding="utf-8"))
        if (
            run_fingerprint(saved, self._manifest["request"]) != fingerprint
            or self._manifest["config"] != saved["config"]
        ):
            raise ValueError("saved run metadata is inconsistent with its recipe fingerprint")
        self._read_audit()
        self.set_status("running", error="")

    def _read_audit(self):
        path = self.directory / "audit.jsonl"
        if not path.exists():
            return
        with path.open("rb+") as handle:
            while True:
                position = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # Discard only an incomplete final append; complete but invalid records must raise an error.
                    handle.truncate(position)
                    handle.flush()
                    os.fsync(handle.fileno())
                    break
                try:
                    event = OperationEvent.model_validate_json(line)
                    if event.run_id != self._manifest["run_id"]:
                        raise ValueError("audit event belongs to another run")
                except ValueError as error:
                    raise ValueError(
                        "audit log is corrupted; refusing to reset request budgets"
                    ) from error
                self._events.append(event)
                self._operation_events[event.logical_call_id].append(event)

    def tasks(self) -> Iterator[dict]:
        return read_jsonl(self.directory / "tasks.jsonl")

    def result_path(self, task_id: str) -> Path:
        name = hashlib.sha256(task_id.encode()).hexdigest()
        return self.directory / "results" / f"{name}.json"

    def completed(self, task_id: str) -> bool:
        return self.result_path(task_id).exists()

    def commit(self, result: TaskResult) -> None:
        write_json(self.result_path(result.id), result)

    def results(self) -> Iterator[TaskResult]:
        for task in self.tasks():
            path = self.result_path(task["id"])
            if path.exists():
                yield TaskResult.model_validate_json(path.read_text(encoding="utf-8"))

    def append_event(self, **fields) -> None:
        event = OperationEvent(
            run_id=self._manifest["run_id"],
            timestamp=datetime.now(timezone.utc).isoformat(),
            **fields,
        )
        with (self.directory / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(encode_json(event.model_dump(by_alias=True)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(event)
        self._operation_events[event.logical_call_id].append(event)

    def record_sample(self, metadata: OperationMetadata, sample: dict) -> dict:
        for event in self._operation_events[metadata.logical_call_id]:
            if event.event == "sample":
                saved = event.data["sample"]
                if content_digest(saved) != content_digest(sample):
                    raise ValueError("replayed sample differs from saved sample")
                return copy.deepcopy(saved)
        self.append_event(**asdict(metadata), event="sample", data={"sample": sample})
        return copy.deepcopy(sample)

    def record_decision(self, metadata: OperationMetadata, decision: QualityDecision) -> None:
        """Save a quality decision without appending duplicate replay events."""
        history = self._operation_events[metadata.logical_call_id]
        if not any(event.event == "quality_decision" for event in history):
            self.append_event(
                **asdict(metadata), event="quality_decision", data=decision.model_dump()
            )

    def set_status(
        self, status: str, *, error: str | None = None, statistics: dict | None = None
    ) -> None:
        if statistics is not None:
            self._manifest["statistics"] = statistics
        self._manifest["status"] = status
        if error is not None:
            self._manifest["error"] = error
        write_json(self.directory / "manifest.json", self._manifest)

    def close(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None
