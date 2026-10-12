"""Run recovery and exports are isolated to the selected temporary directory."""

import json
import tempfile
import unittest
from pathlib import Path

from midasdata.config import Config
from midasdata.io import RunStore, atomic_lines, encode, file_digest, read_jsonl
from midasdata.prompts import VERSIONS
from midasdata.schemas import SampleResult
from tests.support import SOURCE, result_examples


class RunStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "input.jsonl"
        atomic_lines(self.source, [encode({"id": "expanded", "prompt": SOURCE})])
        self.config = Config()
        self.request = {"input": str(self.source), "input_digest": file_digest(self.source)}
        self.store = RunStore(self.root / "run")
        self.addCleanup(self.store.close)
        self.store.prepare("expand", self.config, self.request, None, False)

    def resume(self):
        self.store.close()
        self.store.prepare("expand", self.config, self.request, None, True)

    def append_request(self):
        self.store.append_event(
            sample_id="expanded",
            event="request",
            stage="expand",
            logical_call_id="call",
            technical_attempt=1,
            generation_attempt=1,
            prompt_version=VERSIONS["expand"],
            data={"input": {"source": SOURCE}},
        )

    def test_repeated_resume_does_not_duplicate_audit_or_statistics(self):
        self.append_request()
        for _ in range(2):
            self.resume()
            self.assertEqual(len(self.store.events), 1)
            self.assertEqual(len(self.store.calls["call"]), 1)
            self.assertEqual(self.store.statistics()["requests"], 1)

    def test_result_exports_rebuild_from_committed_results(self):
        result = SampleResult.model_validate(result_examples()[1])
        self.store.commit(result)
        self.store.export("complete")
        self.assertEqual(
            list(read_jsonl(self.store.out / "data.jsonl")), result.model_dump()["records"]
        )
        self.assertEqual(len(list(read_jsonl(self.store.out / "sft.jsonl"))), 3)
        self.assertEqual(list(read_jsonl(self.store.out / "rejected.jsonl")), [])
        expected = (self.store.out / "data.jsonl").read_bytes()
        (self.store.out / "data.jsonl").write_text("damaged export")
        self.resume()
        self.store.export("complete")
        self.assertEqual((self.store.out / "data.jsonl").read_bytes(), expected)

    def test_corrupt_result_preserves_existing_exports_and_removes_temporary_files(self):
        result = SampleResult.model_validate(result_examples()[1])
        self.store.commit(result)
        self.store.export("complete")
        filenames = ("data.jsonl", "lineage.jsonl", "rejected.jsonl", "sft.jsonl", "manifest.json")
        previous = {name: (self.store.out / name).read_bytes() for name in filenames}
        self.store.result_path(result.id).write_text('{"id":')
        with self.assertRaises(ValueError):
            self.store.export("exporting")
        for name in filenames:
            self.assertEqual((self.store.out / name).read_bytes(), previous[name], name)
        self.assertEqual(list(self.store.out.glob("*.tmp")), [])

    def test_rejected_group_exports_no_training_records(self):
        raw = result_examples()[1]
        raw.update(status="rejected", records=[], reason="insufficient_expansion_space")
        result = SampleResult.model_validate(raw)
        self.store.commit(result)
        statistics = self.store.export("complete")
        self.assertEqual(statistics["rejected"], 1)
        self.assertEqual(statistics["pending"], 0)
        self.assertEqual(list(read_jsonl(self.store.out / "data.jsonl")), [])
        self.assertEqual(list(read_jsonl(self.store.out / "sft.jsonl")), [])
        self.assertEqual(list(read_jsonl(self.store.out / "rejected.jsonl")), [result.model_dump()])

    def test_only_unterminated_audit_tail_is_discarded(self):
        self.append_request()
        audit = self.store.out / "audit.jsonl"
        expected = audit.read_bytes()
        with audit.open("ab") as handle:
            handle.write(b'{"event":')
        self.resume()
        self.assertEqual(audit.read_bytes(), expected)
        with audit.open("ab") as handle:
            handle.write(b'{"event":\n')
        with self.assertRaisesRegex(ValueError, "audit log is corrupted"):
            self.resume()

    def test_damaged_task_sequence_blocks_recovery(self):
        (self.store.out / "tasks.jsonl").write_text(
            json.dumps({"id": "other", "prompt": SOURCE}) + "\n"
        )
        with self.assertRaisesRegex(ValueError, "task sequence is corrupted"):
            self.resume()

    def test_live_run_cannot_be_opened_again(self):
        other = RunStore(self.store.out)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(ValueError, "already in use"):
            other.prepare("expand", self.config, self.request, None, True)
        with self.assertRaisesRegex(ValueError, "already open"):
            self.store.prepare("expand", self.config, self.request, None, True)
