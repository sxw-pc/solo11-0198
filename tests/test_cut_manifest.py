from railcar.capture import capture
from railcar.cli import FIXTURES, load
from railcar.cut_manifest import manifest
from railcar.db import current
from support import FieldLedger


class CutWitnessTests(FieldLedger):
    def setUp(self):
        super().setUp()
        self.walk = load(FIXTURES / "cut-walk.json")
        self.cutoff = capture(self.db, self.walk)["last_rev"]

    def review(self, minute=160, cutoff=None):
        return manifest(self.db, "CUT-17", current(self.db) if cutoff is None else cutoff, minute, ("HX-017", "A"))

    def test_replays_earlier_cut_after_opening_a_joint(self):
        original = self.review()
        self.assertEqual(original["status"], "enumerated")
        self.assertEqual([item["car"] for item in original["cars"]], ["HX-017", "HX-071", "HX-099"])
        self.assertEqual(original["cars"][-1]["leading_end"], "B")
        self.ingest(dict(self.walk["events"][-1], id="open-71-99", at=155, state="open"))
        self.assertEqual([item["car"] for item in self.review()["cars"]], ["HX-017", "HX-071"])
        self.assertEqual(self.review(cutoff=self.cutoff), original)

    def test_different_logged_neighbors_for_one_end_block_enumeration(self):
        self.ingest(dict(self.walk["events"][1], id="conflicting-neighbor", second_car="HX-099", second_end="A"))
        self.assertEqual(self.review()["status"], "coupler_conflict")
        self.assertEqual(self.review(minute=149)["cars"], [{"car": "HX-017", "leading_end": "A"}])
