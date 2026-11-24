import sqlite3

from railcar.accrual import audit_run
from railcar.capture import capture
from railcar.cli import FIXTURES, load
from railcar.db import DomainError, current
from support import FieldLedger


class CustodyTests(FieldLedger):
    def test_corrected_clocks_and_aliases_confirm_gate_and_return(self):
        result = self.statement()["result"]
        self.assertEqual(result["delivery"]["at"], 100)
        self.assertEqual([item["minute"] for item in result["delivery"]["observations"]], [100, 100])
        self.assertEqual([item["marks"] for item in result["delivery"]["observations"]],
                         [[["carrier-mark", 4]], [["yard-mark", 5]]])
        self.assertEqual(result["return"]["tender_at"], 185)
        self.assertEqual(result["custody"], [100, 200])
        self.assertEqual(result["status"], "final")

    def test_wrong_gate_cannot_prove_delivery(self):
        self.ingest(self.original_event(10, gate="NORTH-2"))
        result = self.statement()["result"]
        self.assertEqual(result["blockers"], ["delivery"])
        self.assertEqual(result["delivery"]["observations"][1]["status"], "wrong_gate")
        self.assertNotIn("totals", result)

    def test_reused_mark_requires_unique_car_at_corrected_time(self):
        self.ingest(self.original_event(5, id="reused-yard-mark", car="HX-071", begins=100, ends=101))
        held = self.statement()["result"]
        self.assertEqual(held["delivery"]["observations"][1]["status"], "mark_conflict")
        self.ingest(self.original_event(8, offset=0))  # yard reads minute 101, outside [100, 101)
        recovered = self.statement()["result"]
        self.assertEqual(recovered["custody"], [101, 201])
        self.assertEqual(recovered["status"], "final")

    def test_clock_overlap_and_missing_certificate_are_distinct(self):
        self.ingest(self.original_event(8, id="second-cert", offset=4))
        observation = self.statement()["result"]["delivery"]["observations"][1]
        self.assertEqual(observation["status"], "clock_conflict")
        self.assertEqual(len(observation["clocks"]), 2)
        self.ingest(self.original_event(10, device="uncertified"))
        observation = self.statement()["result"]["delivery"]["observations"][1]
        self.assertEqual(observation["status"], "clock_missing")

    def test_pair_tolerance_boundary_and_shared_device(self):
        self.ingest(self.original_event(10, raw=111))
        self.assertEqual(self.statement()["result"]["delivery"]["at"], 110)
        self.ingest(self.original_event(10, raw=112))
        self.assertEqual(self.statement()["result"]["delivery"]["status"], "clock_disagreement")
        self.ingest(self.original_event(10, raw=99, device="R1"))
        self.assertEqual(self.statement()["result"]["delivery"]["status"], "shared_device")

    def test_competing_observation_can_be_withdrawn_without_rewriting_old_run(self):
        raw = self.original_event(10, id="y-in-extra", raw=102)
        self.ingest(raw)
        old = self.statement()
        self.assertEqual(old["result"]["delivery"]["status"], "competing_observations")
        self.ingest({**raw, "active": False})
        self.assertEqual(self.statement()["result"]["status"], "final")
        self.assertEqual(audit_run(self.db, old["run"])["recalculated"], old["result"])

    def test_rejected_or_conflicted_empty_inspection_keeps_clock_running(self):
        self.ingest(self.original_event(11, id="second-check", result="rejected"))
        conflict = self.statement()["result"]
        self.assertEqual(conflict["return"]["status"], "inspection_conflict")
        self.ingest(self.original_event(11, id="second-check", at=186, result="rejected"))
        rejected = self.statement()["result"]
        self.assertEqual(rejected["return"]["status"], "rejected")
        self.assertEqual(rejected["custody"], [100, 210])
        self.assertEqual(rejected["totals"], {"A": 150, "B": 750})
        self.ingest(self.original_event(11, id="third-check", at=199))
        self.assertEqual(self.statement()["result"]["totals"], {"A": 150, "B": 650})

    def test_tender_is_not_exit_and_future_witness_cannot_end_custody(self):
        result = self.statement(as_of=199)["result"]
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["return"]["status"], "tendered")
        self.assertEqual(result["custody"], [100, 199])
        self.assertEqual(self.statement(as_of=200)["result"]["status"], "final")

    def test_exit_before_delivery_and_open_window_limit_cannot_finalize(self):
        self.ingest(self.original_event(12, raw=96), self.original_event(13, raw=99))
        result = self.statement(as_of=270)["result"]
        self.assertEqual(result["return"]["status"], "departure_not_after_delivery")
        self.assertEqual(result["custody"], [100, 240])
        self.assertEqual(result["status"], "provisional")

    def test_overlapping_visits_for_same_car_are_rejected_but_touching_windows_work(self):
        with self.assertRaisesRegex(DomainError, "overlapping visit"):
            self.ingest(self.original_event(3, id="V-18", begins=239, ends=300))
        accepted = self.ingest(self.original_event(3, id="V-18", begins=240, ends=300))
        self.assertEqual(accepted["first_rev"], self.base["last_rev"] + 1)

    def test_application_rejects_invalid_roles_and_decisions_atomically(self):
        bad_values = ((9, "side", "railway"), (9, "phase", "depart"), (15, "side", "incoming"),
                      (15, "decision", "accept"), (19, "side", "carrier"), (19, "decision", "accepted"))
        for revision, field, value in bad_values:
            with self.subTest(revision=revision, field=field), self.assertRaisesRegex(DomainError, field + " must be"):
                self.ingest(self.original_event(8, offset=4), self.original_event(revision, **{field: value}))
        self.assertEqual(current(self.db), self.base["last_rev"])
        self.assertEqual(self.statement()["result"]["totals"], {"A": 150, "B": 650})
        self.ingest(self.original_event(19, decision="withdraw"), self.original_event(15, decision="deny"))
        result = self.statement()["result"]
        self.assertEqual(result["liability"]["handoffs"][0]["status"], "withdrawn")
        self.assertEqual(result["operations"]["incidents"][0]["status"], "denied")

    def test_partial_file_rolls_back_and_same_batch_can_be_fixed_and_retried(self):
        bad = {"kind": "observation", **{key: value for key, value in self.original_event(9).items()
                                        if key != "kind"}, "id": "invalid", "visit": "absent"}
        with self.assertRaises(sqlite3.IntegrityError):
            self.ingest(self.original_event(8, offset=4), bad)
        self.assertEqual(current(self.db), self.base["last_rev"])
        correction = self.ingest(self.original_event(8, offset=4), self.original_event(7, offset=4))
        self.assertEqual(correction["first_rev"], self.base["last_rev"] + 1)
        with self.assertRaisesRegex(DomainError, "batch boundary"):
            self.statement(cutoff=correction["first_rev"])
        self.assertEqual(self.statement()["result"]["custody"], [105, 205])

    def test_same_batch_is_idempotent_but_different_payload_cannot_reuse_id(self):
        document = load(FIXTURES / "station.json")
        self.assertTrue(capture(self.db, document)["replayed"])
        document["events"][0]["id"] = "another"
        with self.assertRaisesRegex(DomainError, "different payload"):
            capture(self.db, document)
        self.assertEqual(current(self.db), self.base["last_rev"])
