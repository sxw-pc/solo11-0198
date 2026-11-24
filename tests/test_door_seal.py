from railcar.cli import FIXTURES, load
from railcar.capture import capture
from railcar.db import DomainError, snapshot, visit_at
from railcar.door_seal import trace
from support import FieldLedger


class SealTests(FieldLedger):
    def receipt(self, cutoff=None, as_of=210):
        selected = snapshot(self.db, cutoff)
        return trace(self.db, visit_at(self.db, "V-17", selected), "left", selected, as_of)

    def test_full_serial_trace_and_late_damage(self):
        document = load(FIXTURES / "door-seals.json")
        recorded = capture(self.db, document)
        self.assertTrue(capture(self.db, document)["replayed"])
        self.assertEqual(self.receipt()["seal"], "NW-00972")
        self.assertEqual(self.receipt(as_of=199)["state"], "awaiting_crossings")
        self.ingest({**document["events"][1], "condition": "broken"})
        self.assertEqual(self.receipt()["state"], "exception")
        self.assertEqual(self.receipt(recorded["last_rev"])["state"], "continuous")
        self.assertEqual(self.statement()["result"]["status"], "final")

    def test_outside_crossing_and_scope_changes(self):
        document = load(FIXTURES / "door-seals.json")
        capture(self.db, document)
        outgoing = document["events"][2]
        self.ingest({**outgoing, "at": 201})
        self.assertEqual(self.receipt()["state"], "awaiting_observations")
        before = snapshot(self.db, None)
        with self.assertRaisesRegex(DomainError, "different visit, door or checkpoint"):
            self.ingest({**outgoing, "door": "right"})
        self.assertEqual(snapshot(self.db, None), before)
