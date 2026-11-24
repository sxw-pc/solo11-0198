from railcar.accrual import audit_run
from support import FieldLedger


class OperationsTests(FieldLedger):
    def test_dual_approval_with_available_capacity_does_not_pause(self):
        result = self.statement()["result"]
        incident = result["operations"]["incidents"][0]
        self.assertEqual(incident["status"], "approved")
        self.assertEqual(incident["attestations"], [["carrier", 16], ["yard", 15]])
        self.assertFalse(any(part["paused"] for part in result["operations"]["segments"]))
        self.assertEqual(result["totals"], {"A": 150, "B": 650})

    def test_zero_capacity_changes_both_payers_and_original_run_replays(self):
        original = self.statement()
        self.ingest(self.original_event(17, slots=0))
        corrected = self.statement()["result"]
        self.assertEqual(corrected["totals"], {"B": 500})
        self.assertEqual([(part["from"], part["to"]) for part in corrected["slices"] if part["paused"]],
                         [(120, 135), (135, 150)])
        self.assertEqual(audit_run(self.db, original["run"])["recalculated"], original["result"])

    def test_capacity_conflict_only_resumes_the_overlapping_minutes(self):
        self.ingest(self.original_event(17, slots=0),
                    self.original_event(17, id="fallback-bay", begins=130, ends=140, slots=2))
        result = self.statement()["result"]
        segments = result["operations"]["segments"]
        self.assertEqual([(p["from"], p["to"], p["capacity"], p["paused"]) for p in segments[1:-1]],
                         [(120, 130, "zero", True), (130, 140, "conflict", False), (140, 150, "zero", True)])
        self.assertEqual(result["totals"], {"A": 50, "B": 550})

    def test_overlapping_approved_incidents_count_each_minute_once(self):
        second = self.revision(**self.original_event(14, id="belt-8", begins=130, ends=160))
        self.ingest(self.original_event(15, incident="belt-8", incident_rev=second),
                    self.original_event(16, incident="belt-8", incident_rev=second),
                    self.original_event(17, ends=160, slots=0))
        result = self.statement()["result"]
        self.assertEqual(sum(p["to"] - p["from"] for p in result["slices"] if p["paused"]), 40)
        self.assertEqual(result["totals"], {"B": 400})

    def test_corrected_incident_needs_fresh_approvals_and_late_old_denial_isolated(self):
        revised = self.revision(**self.original_event(14, begins=125))
        self.ingest(self.original_event(17, slots=0), self.original_event(15, incident_rev=revised))
        waiting = self.statement()["result"]
        self.assertEqual(waiting["operations"]["incidents"][0]["status"], "awaiting_both")
        self.assertEqual(waiting["totals"], {"A": 150, "B": 650})
        self.ingest(self.original_event(16, incident_rev=revised))
        approved = self.statement()["result"]
        self.assertEqual(approved["totals"], {"A": 50, "B": 500})
        self.ingest(self.original_event(16, decision="deny"))  # refers to revision 14, not revised
        self.assertEqual(self.statement()["result"], approved)
        self.ingest(self.original_event(16, incident_rev=revised, decision="deny"))
        denied = self.statement()["result"]
        self.assertEqual(denied["operations"]["incidents"][0]["status"], "denied")
        self.assertEqual(denied["totals"], {"A": 150, "B": 650})

    def test_zero_capacity_does_not_excuse_carrier_caused_interruption(self):
        revised = self.revision(**self.original_event(14, cause="carrier"))
        self.ingest(self.original_event(17, slots=0), self.original_event(15, incident_rev=revised),
                    self.original_event(16, incident_rev=revised))
        result = self.statement()["result"]
        self.assertEqual(result["operations"]["incidents"][0]["status"], "ineligible_cause")
        self.assertEqual(result["totals"], {"A": 150, "B": 650})
