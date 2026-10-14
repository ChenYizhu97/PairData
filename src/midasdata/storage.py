"""运行目录、原子文件写入、审计日志和任务提交；不执行工作流。

Run directories, atomic writes, audit logs and task commits; workflow execution lives elsewhere.
"""

import fcntl
import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from midasdata import __version__, prompts
from midasdata.config import RunSettings
from midasdata.limits import WORKFLOW_REVISION
from midasdata.schemas import OperationEvent, QualityDecision, TaskResult


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


def run_fingerprint(settings: RunSettings, request: dict, sampler: dict | None) -> str:
    return content_digest(
        {
            "config": settings.model_dump(exclude={"concurrency", "preview_page_size"}),
            "request": request,
            "sampler": sampler,
            "prompt_digest": file_digest(Path(prompts.__file__)),
            "version": __version__,
            "workflow_revision": WORKFLOW_REVISION,
        }
    )


class RunStore:
    """保存一次运行的配置、任务、调用记录和结果，支持中断后恢复。

    Store configuration, tasks, call events and results for a run and support resuming it.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.manifest: dict = {}
        self.events: list[dict] = []
        self.operation_events: dict[str, list[dict]] = defaultdict(list)
        self._lock: TextIO | None = None

    def open(
        self,
        command: str,
        settings: RunSettings,
        request: dict,
        sampler: dict | None,
        *,
        resume: bool,
        run_id: str,
        tasks: Iterable[dict],
    ) -> None:
        if self._lock is not None:
            raise ValueError("run store is already open")
        expected = run_fingerprint(settings, request, sampler)
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
        self.events = []
        self.operation_events = defaultdict(list)
        if resume:
            self._resume(command, expected)
        else:
            self._initialize(command, settings, request, sampler, expected, run_id, tasks)

    def _initialize(self, command, settings, request, sampler, fingerprint, run_id, tasks):
        (self.directory / "results").mkdir()
        # 整个输入完成验证和写入后，runner 才能开始请求模型。
        # The runner can request models only after all input has been validated and persisted.
        write_jsonl(self.directory / "tasks.jsonl", tasks)
        self.manifest = {
            "run_id": run_id,
            "workflow": command,
            "workflow_revision": WORKFLOW_REVISION,
            "version": __version__,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": settings.model_dump(),
            "request": request,
            "fingerprint": fingerprint,
            "tasks_digest": file_digest(self.directory / "tasks.jsonl"),
            "prompt_digest": file_digest(Path(prompts.__file__)),
            "planned": sum(1 for _ in self.tasks()),
        }
        write_json(self.directory / "manifest.json", self.manifest)
        write_json(
            self.directory / "sources.json",
            {
                "type": "llm" if command == "generate" else "jsonl",
                "input": request,
                "sampler": sampler,
                "version": __version__,
                "workflow_revision": WORKFLOW_REVISION,
            },
        )

    def _resume(self, command: str, fingerprint: str):
        self.manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        if (
            self.manifest["workflow"] != command
            or self.manifest.get("workflow_revision") != WORKFLOW_REVISION
            or self.manifest["fingerprint"] != fingerprint
        ):
            raise ValueError("resume workflow/version/input/config/prompt mismatch")
        if file_digest(self.directory / "tasks.jsonl") != self.manifest["tasks_digest"]:
            raise ValueError("saved task sequence is corrupted")
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
                    # 仅丢弃未完成的最后一次追加，完整但非法的记录必须报错。
                    # Discard only an incomplete final append; complete but invalid records must raise an error.
                    handle.truncate(position)
                    handle.flush()
                    os.fsync(handle.fileno())
                    break
                try:
                    event = OperationEvent.model_validate_json(line).model_dump()
                    if event["run_id"] != self.manifest["run_id"]:
                        raise ValueError("audit event belongs to another run")
                except ValueError as error:
                    raise ValueError(
                        "audit log is corrupted; refusing to reset request budgets"
                    ) from error
                self.events.append(event)
                self.operation_events[event["logical_call_id"]].append(event)

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
            run_id=self.manifest["run_id"],
            timestamp=datetime.now(timezone.utc).isoformat(),
            **fields,
        ).model_dump()
        with (self.directory / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(encode_json(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.events.append(event)
        self.operation_events[event["logical_call_id"]].append(event)

    def record_decision(self, metadata: dict, decision: QualityDecision) -> None:
        """保存节点已计算的质量结果，重放时避免重复记录。

        Save a quality decision already computed by a node, without duplicate replay events.
        """
        history = self.operation_events[metadata["logical_call_id"]]
        if not any(event["event"] == "quality_decision" for event in history):
            self.append_event(**metadata, event="quality_decision", data=decision.model_dump())

    def set_status(self, status: str, *, error: str | None = None) -> None:
        self.manifest["status"] = status
        if error is not None:
            self.manifest["error"] = error
        write_json(self.directory / "manifest.json", self.manifest)

    def close(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None
