"""Recovery contracts at the on-disk boundary."""

import unittest

from pairdata.execution import operation_metadata
from pairdata.storage import RunStore, content_digest, read_jsonl
from tests.fakes import RecipeFixture


class StorageTest(RecipeFixture, unittest.TestCase):
    def open_store(self, store, *, resume=False):
        store.open(
            self.recipe.snapshot(),
            {"count": 0, "seed": 42},
            resume=resume,
            run_id="test",
            tasks=[],
        )

    def setUp(self):
        super().setUp()
        self.store = RunStore(self.root / "run")
        self.open_store(self.store)
        self.addCleanup(self.store.close)
        self.metadata = operation_metadata("task-1", "source", "sample")
        self.store.record_sample(self.metadata, {"topic": "meeting"})

    def test_audit_keeps_legacy_names_and_logical_ids(self):
        event = next(read_jsonl(self.store.directory / "audit.jsonl"))
        self.assertEqual(event["sample_id"], "task-1")
        self.assertEqual(event["stage"], "source.sample")
        self.assertEqual(event["technical_attempt"], 0)
        self.assertEqual(event["logical_call_id"], content_digest(["task-1", "source.sample", 0]))
        self.assertNotIn("task_id", event)
        self.store.close()
        self.open_store(self.store, resume=True)
        restored = self.store.operation_history(self.metadata.logical_call_id)[0]
        self.assertEqual(restored.task_id, "task-1")
        self.assertEqual(restored.operation, "source.sample")
        self.assertEqual(restored.request_attempt, 0)

    def test_directory_lock_rejects_second_writer(self):
        competing = RunStore(self.store.directory)
        self.addCleanup(competing.close)
        with self.assertRaisesRegex(ValueError, "already in use"):
            self.open_store(competing, resume=True)
        self.store.close()
        self.open_store(competing, resume=True)

    def test_incomplete_audit_tail_is_discarded(self):
        path = self.store.directory / "audit.jsonl"
        original = path.read_bytes()
        self.store.close()
        with path.open("ab") as handle:
            handle.write(b'{"incomplete":')
        self.open_store(self.store, resume=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(list(self.store.iter_events())), 1)

    def test_complete_corrupt_line_stops_resume_and_releases_lock(self):
        path = self.store.directory / "audit.jsonl"
        original = path.read_bytes()
        self.store.close()
        path.write_bytes(original + b'{"invalid":true}\n')
        with self.assertRaisesRegex(ValueError, "audit log is corrupted"):
            self.open_store(self.store, resume=True)
        self.assertEqual(path.read_bytes(), original + b'{"invalid":true}\n')
        path.write_bytes(original)
        # A failed open must release its lock and allow the same store to reopen.
        self.open_store(self.store, resume=True)
