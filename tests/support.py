"""Isolated field ledger from the same JSON used by the CLI."""
import unittest

from railcar.accrual import record_run
from railcar.capture import capture
from railcar.cli import FIXTURES, load, seed
from railcar.db import connect, initialize
from railcar.settlement import file_invoice


class FieldLedger(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        initialize(self.db)
        self.base = seed(self.db)
        self.next_batch = 1

    def ingest(self, *events):
        result = capture(self.db, {"batch": f"test-{self.next_batch}", "events": list(events)})
        self.next_batch += 1
        return result

    def revision(self, **event):
        return self.ingest(event)["last_rev"]

    def original_event(self, revision, **changes):
        event = load(FIXTURES / "station.json")["events"][revision - 1]
        return {**event, **changes}

    def statement(self, as_of=210, cutoff=None):
        return record_run(self.db, "V-17", as_of, cutoff)

    def invoice(self):
        return file_invoice(self.db, load(FIXTURES / "invoice.json"))
