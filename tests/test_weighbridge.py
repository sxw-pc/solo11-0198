import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from railcar.accrual import record_run
from railcar.db import current
from support import FieldLedger
from railcar.weighbridge import RESIDUAL_LIMIT_KG, evaluate_visit, intake


ROOT = Path(__file__).resolve().parents[1]

# Worked example: gross 1015 kg, tare 1000 kg, zero +/-4 kg per scale,
# independent read error +/-2 kg per ticket.
GROSS_READING, TARE_READING = 1015, 1000
ZERO_BAND = (-4, 4)
READ_BAND = (-2, 2)
GROSS_AT, TARE_AT = 170, 175


def zero_voucher(vid, scale, at_from=0, at_to=400, band=ZERO_BAND, active=True, **changes):
    event = {"kind": "zero_voucher", "id": vid, "device": scale,
             "valid_from": at_from, "valid_to": at_to,
             "zero_min": band[0], "zero_max": band[1], "active": active}
    return {**event, **changes}


def ticket(tid, phase, at, reading, scale="W1", band=READ_BAND, visit="V-17", car="HX-017",
           station="NORTH", window=(0, 400), active=True, **changes):
    event = {"kind": "weigh_ticket", "id": tid, "visit": visit, "car": car, "station": station,
             "scale": scale, "phase": phase, "at": at, "reading": reading,
             "read_min": reading + band[0], "read_max": reading + band[1],
             "window_from": window[0], "window_to": window[1], "active": active}
    return {**event, **changes}


def gross(tid="w-gross", at=GROSS_AT, reading=GROSS_READING, **changes):
    return ticket(tid, "gross", at, reading, **changes)


def tare(tid="w-tare", at=TARE_AT, reading=TARE_READING, **changes):
    return ticket(tid, "tare", at, reading, **changes)


class WeighbridgeTests(FieldLedger):
    def weigh(self, *events, batch=None, preview=False, as_of=None):
        batch = batch or f"weigh-{self.next_batch}"
        result = intake(self.db, {"batch": batch, "events": list(events)},
                        preview=preview, as_of=as_of)
        if not preview:
            self.next_batch += 1
        return result

    def preview_visit(self, visit="V-17", as_of=None):
        return evaluate_visit(self.db, visit, as_of=as_of)

    def assert_refs(self, chain, table_id):
        self.assertTrue(any(ref[0] == table_id for ref in chain), chain)

    def weigh_inspections(self):
        # Only inspections imported by the weighbridge command; the seeded
        # manual empty-check is outside this command's scope.
        return self.db.execute(
            "SELECT * FROM inspection WHERE id LIKE 'weigh-%'").fetchall()

    # ------------------------------------------------------------------ #
    # Worked example: independent weighings versus a continuous pair
    # ------------------------------------------------------------------ #

    def test_independent_weighings_keep_two_zero_errors_and_cannot_sign_off(self):
        # Two separate vouchers on the same scale, split between the readings.
        result = self.weigh(
            zero_voucher("z-gross", "W1", at_from=0, at_to=173),
            zero_voucher("z-tare", "W1", at_from=173, at_to=400),
            gross(), tare(),
        )
        self.assertIsNone(result["inspection"])
        verdict = result["result"]
        self.assertEqual(verdict["status"], "inconclusive")
        self.assertEqual(verdict["zero_mode"], "independent_zero")
        # Independent: 15 +/- 4 (two read errors) +/- 8 (two independent zero
        # errors) gives every residual in [3, 27], which crosses 20 kg.
        self.assertEqual(verdict["residual_kg"], [3, 27])
        self.assertIn("crosses", verdict["reason"])
        self.assertEqual(self.preview_visit()["status"], "inconclusive")
        self.assertEqual(len(self.weigh_inspections()), 0)

    def test_indifferent_independent_band_can_still_decide_when_wide_enough(self):
        # Same split-voucher structure, but a clearly empty car: even the worst
        # independent combination stays under the allowance, so ready imports.
        result = self.weigh(
            zero_voucher("z-gross-2", "W2", at_from=0, at_to=173),
            zero_voucher("z-tare-2", "W2", at_from=173, at_to=400),
            gross("g-ind", scale="W2", reading=1005),
            tare("t-ind", scale="W2", reading=1000),
            batch="weigh-independent-ready",
        )
        # 5 +/- 4 (read) +/- 8 (zero) = [-7, 17]: every value <= 20.
        self.assertEqual(result["result"]["zero_mode"], "independent_zero")
        self.assertEqual(result["result"]["residual_kg"], [-7, 17])
        self.assertEqual(result["result"]["status"], "ready")
        self.assertEqual(result["inspection"]["result"], "ready")

    def test_continuous_zero_pair_subtracts_one_shared_offset_and_signs_off_ready(self):
        result = self.weigh(zero_voucher("z-cont", "W1"), gross(), tare())
        verdict = result["result"]
        self.assertEqual(verdict["status"], "ready")
        self.assertEqual(verdict["zero_mode"], "paired_shared_zero")
        # The shared zero cancels in the gross-minus-tare difference; only the
        # two independent +/-2 kg read errors remain: 15 +/- 4 = [11, 19].
        self.assertEqual(verdict["residual_kg"], [11, 19])
        self.assertTrue(verdict["residual_kg"][1] <= RESIDUAL_LIMIT_KG)
        inspection = result["inspection"]
        self.assertEqual(inspection["result"], "ready")
        self.assertEqual(inspection["at"], TARE_AT)
        chain = inspection["evidence_chain"]
        self.assert_refs(chain, "w-gross")
        self.assert_refs(chain, "w-tare")
        self.assert_refs(chain, "z-cont")
        # Raw rows, voucher and generated inspection are mutually traceable.
        self.assertEqual(self.preview_visit()["evidence_chain"], chain)
        stored = self.db.execute("SELECT * FROM inspection WHERE rev = ?",
                                 (inspection["inspection"][1],)).fetchone()
        self.assertEqual(stored["result"], "ready")

    def test_rezero_between_the_two_tickets_forces_pending_even_though_paired_values_pass(self):
        # The same two tickets that sign off under one continuous voucher must
        # return to evidence-pending once a valid re-zeroing sits between them.
        self.weigh(
            zero_voucher("z-first", "W1", at_from=0, at_to=173),
            zero_voucher("z-second", "W1", at_from=173, at_to=400),
            gross(), tare(),
            batch="weigh-pair-1",
        )
        self.assertEqual(self.preview_visit()["zero_mode"], "independent_zero")
        self.assertEqual(self.preview_visit()["status"], "inconclusive")
        self.assertEqual(self.preview_visit()["residual_kg"], [3, 27])
        self.assertEqual(len(self.weigh_inspections()), 0)

        # Repairing with one continuous voucher covering two fresh readings
        # (and withdrawing the old pair) restores the pairing and signs off.
        fixed = self.weigh(
            zero_voucher("z-fixed", "W3", at_from=0, at_to=400),
            gross(active=False), tare(active=False),
            gross("w-gross-2", scale="W3"), tare("w-tare-2", scale="W3"),
            batch="weigh-pair-2",
        )
        self.assertEqual(fixed["result"]["zero_mode"], "paired_shared_zero")
        self.assertEqual(fixed["inspection"]["result"], "ready")

    def test_two_vouchers_covering_one_ticket_is_a_conflict_not_a_pairing(self):
        result = self.weigh(
            zero_voucher("z-a", "W1"),
            zero_voucher("z-b", "W1", at_from=160, at_to=200),
            gross(), tare(),
        )
        statuses = {item["ticket"][0]: item["status"] for item in result["result"]["tickets"]}
        self.assertEqual(set(statuses.values()), {"zero_conflict"})
        self.assertIsNone(result["inspection"])

    # ------------------------------------------------------------------ #
    # Residual classification against the 20 kg allowance
    # ------------------------------------------------------------------ #

    def test_all_values_above_limit_generate_rejected(self):
        rejected = self.weigh(
            zero_voucher("z-r", "W1"),
            gross("g-r", reading=1030), tare("t-r", reading=1000),
            batch="weigh-reject",
        )
        # Paired shared zero cancels: residual = 30 +/- 4 = [26, 34] > 20.
        self.assertEqual(rejected["result"]["status"], "rejected")
        self.assertEqual(rejected["result"]["residual_kg"], [26, 34])
        self.assertEqual(rejected["inspection"]["result"], "rejected")

    def test_all_values_within_limit_generate_ready(self):
        ready = self.weigh(
            zero_voucher("z-ok", "W2"),
            gross("g-ok", scale="W2", reading=1010), tare("t-ok", scale="W2", reading=1000),
            batch="weigh-ready",
        )
        # Residual = 10 +/- 4 = [6, 14], every possible value within 20 kg.
        self.assertEqual(ready["result"]["status"], "ready")
        self.assertEqual(ready["result"]["residual_kg"], [6, 14])
        self.assertEqual(ready["inspection"]["result"], "ready")

    def test_interval_straddling_the_limit_stays_pending_without_inspection(self):
        # Independent zero errors widen the band enough to cross the limit.
        crossing = self.weigh(
            zero_voucher("z-gross-w", "W4", at_from=0, at_to=173),
            zero_voucher("z-tare-w", "W4", at_from=173, at_to=400),
            gross("g-wide", scale="W4"), tare("t-wide", scale="W4"),
            batch="weigh-straddle",
        )
        # 15 +/- 4 read +/- 8 zero = [3, 27]: the limit 20 sits inside.
        self.assertEqual(crossing["result"]["status"], "inconclusive")
        self.assertEqual(crossing["result"]["residual_kg"], [3, 27])
        self.assertIsNone(crossing["inspection"])

    def test_boundary_value_equal_to_the_allowance_is_still_ready(self):
        # Residual high endpoint lands exactly on 20 kg: within the allowance.
        result = self.weigh(
            zero_voucher("z-edge", "W5"),
            gross("g-edge", scale="W5", reading=1016),
            tare("t-edge", scale="W5", reading=1000),
            batch="weigh-edge",
        )
        self.assertEqual(result["result"]["residual_kg"], [12, 20])
        self.assertEqual(result["result"]["status"], "ready")

    # ------------------------------------------------------------------ #
    # Ticket scope: car, station and weighing window cannot be borrowed
    # ------------------------------------------------------------------ #

    def test_other_car_station_or_window_cannot_lend_its_tare(self):
        cases = [
            ("other-car", tare("t-car", car="HX-071"), "different_car"),
            ("other-station", tare("t-station", station="SOUTH"), "different_station"),
            ("other-window", tare("t-window", window=(0, 175)), "outside_weighing_window"),
        ]
        for name, bad_tare, status in cases:
            with self.subTest(name=name):
                result = self.weigh(zero_voucher(f"z-{name}", "W1"), gross(f"g-{name}"),
                                    bad_tare, batch=f"weigh-{name}")
                self.assertEqual(result["result"]["status"], "evidence_missing")
                detail = next(item for item in result["result"]["tickets"]
                              if item["ticket"][0] == bad_tare["id"])
                self.assertEqual(detail["status"], status)
                self.assertIsNone(result["inspection"])

        # A tare recorded under a later visit of the same car cannot be
        # borrowed for V-17: each batch covers one visit, and the V-17 preview
        # only counts tickets whose visit scope matches.
        self.ingest(self.original_event(3, id="V-18", begins=240, ends=300))
        self.weigh(
            zero_voucher("z-v18", "W7"),
            gross("g-v18", visit="V-18", scale="W7", at=250),
            tare("t-v18", visit="V-18", scale="W7", at=255),
            batch="weigh-v18",
        )
        view = self.preview_visit("V-17")
        self.assertNotIn("t-v18", [item["ticket"][0] for item in view["tickets"]])
        self.assertEqual(view["status"], "evidence_missing")
        self.assertEqual(self.preview_visit("V-18")["status"], "ready")

    def test_ticket_outside_visit_window_and_missing_voucher_are_distinct(self):
        # Voucher ends at 172: the tare at 173 is still inside the visit window
        # but has no covering zero certificate, so zero_missing wins.
        result = self.weigh(
            zero_voucher("z-narrow", "W1", at_from=0, at_to=172),
            gross("g-narrow", at=170), tare("t-narrow", at=173),
            batch="weigh-window",
        )
        by_id = {item["ticket"][0]: item["status"] for item in result["result"]["tickets"]}
        self.assertEqual(by_id["t-narrow"], "zero_missing")
        self.assertEqual(by_id["g-narrow"], "eligible")
        # A weighing beyond the visit window (V-17 ends at 240, exclusive) is a
        # different, earlier failure than a missing certificate.
        outside = self.weigh(
            zero_voucher("z-out", "W8"),
            gross("g-out", scale="W8", at=241), tare("t-out", scale="W8", at=242),
            batch="weigh-outside",
        )
        by_id = {item["ticket"][0]: item["status"] for item in outside["result"]["tickets"]}
        self.assertEqual(by_id["g-out"], "outside_visit_window")
        self.assertEqual(by_id["t-out"], "outside_visit_window")

    def test_competing_tickets_block_and_withdrawal_recovers_without_rewriting(self):
        first = self.weigh(zero_voucher("z-comp", "W1"),
                           gross("g1"), gross("g2", at=171), tare(),
                           batch="weigh-competing")
        self.assertEqual(first["result"]["status"], "competing_tickets")
        self.assertIsNone(first["inspection"])
        recovered = self.weigh(gross("g2", at=171, active=False), batch="weigh-withdraw")
        self.assertEqual(recovered["result"]["status"], "ready")
        self.assertEqual(recovered["inspection"]["result"], "ready")

    def test_tare_before_gross_is_rejected_as_evidence(self):
        result = self.weigh(zero_voucher("z-order", "W1"),
                            gross("g-order", at=176), tare("t-order", at=175),
                            batch="weigh-order")
        self.assertEqual(result["result"]["status"], "tare_before_gross")
        self.assertIsNone(result["inspection"])

    # ------------------------------------------------------------------ #
    # Exit boundary: later weighing evidence cannot patch a passed departure
    # ------------------------------------------------------------------ #

    def _exit_boundary(self):
        # Seeded V-17 out observations correct to minutes 200 (carrier raw 198
        # + offset 2) and 200 (yard raw 201 - 1): earliest exit reading 200.
        departure = record_run(self.db, "V-17", 210)["result"]["return"]["crossing"]
        self.assertEqual(departure["status"], "confirmed")
        return departure["earliest"]

    def test_tare_after_earliest_exit_reading_cannot_become_ready(self):
        earliest = self._exit_boundary()
        self.assertEqual(earliest, 200)
        result = self.weigh(
            zero_voucher("z-late", "W1"),
            gross("g-late", at=199), tare("t-late", at=201),
            batch="weigh-late",
        )
        self.assertEqual(result["result"]["status"], "after_departure")
        self.assertIsNone(result["inspection"])
        # The raw rows are still stored and traceable; only the import is blocked.
        self.assertEqual(len(self.db.execute("SELECT * FROM weigh_ticket").fetchall()), 2)

    def test_ready_before_exit_proves_return_and_ends_the_proven_occupation(self):
        earliest = self._exit_boundary()
        # Weigh after the seeded manual check at 185 so the imported inspection
        # is itself the controlling one, and well before the exit at 200.
        result = self.weigh(zero_voucher("z-good", "W1"),
                            gross("g-good", at=188), tare("t-good", at=190),
                            batch="weigh-good")
        self.assertEqual(result["inspection"]["at"], 190)
        self.assertLess(190, earliest)
        statement = record_run(self.db, "V-17", 210)["result"]
        self.assertEqual(statement["return"]["status"], "returned")
        self.assertEqual(statement["return"]["tender_at"], 190)
        self.assertEqual(statement["custody"], [100, 200])

    def test_rejected_before_exit_keeps_the_car_occupied(self):
        self.weigh(zero_voucher("z-bad", "W1"),
                   gross("g-bad", at=188, reading=1030),
                   tare("t-bad", at=190, reading=1000),
                   batch="weigh-bad")
        statement = record_run(self.db, "V-17", 210)["result"]
        self.assertEqual(statement["return"]["status"], "rejected")
        self.assertEqual(statement["custody"], [100, 210])

    def test_later_ready_cannot_override_an_exit_that_already_happened(self):
        # A late, numerically sufficient pair arrives after the exit boundary.
        late = self.weigh(
            zero_voucher("z-late2", "W2"),
            gross("g-late2", scale="W2", at=199, reading=1010),
            tare("t-late2", scale="W2", at=201, reading=1000),
            batch="weigh-late2",
        )
        self.assertEqual(late["result"]["status"], "after_departure")
        self.assertIsNone(late["inspection"])
        statement = record_run(self.db, "V-17", 210)["result"]
        # The seeded manual ready inspection at 185 still governs the return.
        self.assertEqual(statement["return"]["status"], "returned")
        self.assertEqual(statement["return"]["inspections"], [["empty-check", 11]])

    def test_preview_writes_nothing_and_identifies_what_would_be_stored(self):
        document = {"batch": "weigh-preview-only", "events": [
            zero_voucher("z-p", "W1"), gross("g-p"), tare("t-p")]}
        before = current(self.db)
        result = intake(self.db, document, preview=True)
        self.assertTrue(result["preview"])
        self.assertEqual(result["result"]["status"], "ready")
        self.assertEqual(result["would_write_revisions"], [before + 1, before + 3])
        self.assertIsNotNone(result["inspection"])
        self.assertEqual(current(self.db), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM weigh_ticket").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM weigh_batch").fetchone()[0], 0)
        # Running it for real then yields exactly the previewed verdict.
        committed = intake(self.db, document)
        self.assertEqual(committed["result"]["status"], "ready")
        self.assertEqual(committed["inspection"]["result"], "ready")

    def test_same_batch_replays_and_a_changed_payload_is_rejected(self):
        document = {"batch": "weigh-idem", "events": [
            zero_voucher("z-i", "W1"), gross("g-i"), tare("t-i")]}
        first = intake(self.db, document)
        self.assertFalse(first["replayed"])
        again = intake(self.db, document)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["inspection"]["inspection"], first["inspection"]["inspection"])
        document["events"][1] = gross("g-i", reading=1016)
        with self.assertRaisesRegex(Exception, "different payload"):
            intake(self.db, document)

    def test_every_revision_is_traceable_from_raw_rows_to_the_inspection(self):
        result = self.weigh(zero_voucher("z-trace", "W1"),
                            gross("g-trace"), tare("t-trace"), batch="weigh-trace")
        revs = result["revisions"]
        self.assertEqual(len(revs), 3)
        chain = result["inspection"]["evidence_chain"]
        # The chain cites raw ticket revs and the voucher rev; each resolves to
        # its original payload through the revision ledger.
        for ref_id, rev in chain:
            row = self.db.execute("SELECT action, payload FROM revision WHERE rev = ?",
                                  (rev,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["action"],
                             "zero_voucher" if ref_id == "z-trace" else "weigh_ticket")
        inspection_rev = result["inspection"]["inspection"][1]
        self.assertGreater(inspection_rev, max(revs))


class WeighbridgeCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix=".weigh-test-", dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "shift.sqlite"
        self.file = Path(directory.name) / "round.json"

    def command(self, *args, code=0):
        process = subprocess.run([sys.executable, "-B", "-m", "railcar", "--db", str(self.path), *args],
                                 cwd=ROOT, text=True, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, code, process.stderr)
        return process.stderr if code else json.loads(process.stdout)

    def test_weigh_preview_and_intake_round_trip_through_the_cli(self):
        self.command("init")
        self.command("seed")
        self.file.write_text(json.dumps({"batch": "cli-round", "events": [
            zero_voucher("z-cli", "W1"), gross("g-cli"), tare("t-cli")]}), encoding="utf-8")
        preview = self.command("weigh-preview", str(self.file))
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["result"]["status"], "ready")
        self.assertEqual(preview["result"]["residual_kg"], [11, 19])
        # Preview must not have moved the revision head (21 seeded revisions).
        self.assertEqual(self.command("evidence", "21")["event"]["kind"], "tariff")
        committed = self.command("weigh", str(self.file))
        self.assertEqual(committed["inspection"]["result"], "ready")
        view = self.command("weighing", "V-17")
        self.assertEqual(view["result"]["status"], "ready")
        self.assertEqual(view["result"]["residual_kg"], [11, 19])

    def test_weigh_preview_is_read_only_across_process_restarts(self):
        self.command("init")
        self.command("seed")
        self.file.write_text(json.dumps({"batch": "cli-preview", "events": [
            zero_voucher("z-cp", "W1"), gross("g-cp"), tare("t-cp")]}), encoding="utf-8")
        self.command("weigh-preview", str(self.file))
        # Nothing was committed: the batch id is unknown and the head did not move.
        self.command("evidence", "21")
        committed = self.command("weigh", str(self.file))
        self.assertEqual(committed["batch"], "cli-preview")
        # Replaying the same file through the CLI returns the stored result.
        replay = self.command("weigh", str(self.file))
        self.assertTrue(replay["replayed"])


if __name__ == "__main__":
    unittest.main()
