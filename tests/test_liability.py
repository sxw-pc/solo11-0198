from railcar.accrual import audit_run
from support import FieldLedger


class LiabilityTests(FieldLedger):
    def test_dual_handoff_allocates_at_later_signature_and_shares_free_time(self):
        self.ingest(self.original_event(21, free=40))
        result = self.statement()["result"]
        self.assertEqual(result["liability"]["spans"], [{"from": 100, "to": 135, "payer": "A"},
                                                        {"from": 135, "to": 200, "payer": "B"}])
        self.assertEqual(sum(p["free_used"] for p in result["slices"]), 40)
        self.assertEqual(result["totals"], {"B": 600})

    def test_clock_correction_crosses_tariff_boundary_without_repricing_old_run(self):
        self.ingest(self.original_event(21, ends=105), self.original_event(21, id="new-tariff", begins=105, rate=20))
        original = self.statement()
        self.assertEqual(original["result"]["totals"], {"A": 150, "B": 650})
        self.ingest(self.original_event(8, offset=4))
        revised = self.statement()["result"]
        self.assertEqual(revised["custody"], [105, 205])
        self.assertEqual(revised["tariff"]["rate"], 20)
        self.assertEqual(revised["totals"], {"A": 200, "B": 1400})
        self.assertTrue(audit_run(self.db, original["run"])["matches"])

    def test_overlapping_or_missing_tariff_prevents_a_false_zero_bill(self):
        self.ingest(self.original_event(21, id="other-tariff", begins=100, ends=120))
        conflict = self.statement()["result"]
        self.assertEqual(conflict["tariff"]["status"], "conflict")
        self.assertNotIn("totals", conflict)
        self.ingest(self.original_event(21, begins=101), self.original_event(21, id="other-tariff", begins=101))
        self.assertEqual(self.statement()["result"]["tariff"]["status"], "missing")

    def test_current_handoff_requires_current_signatures_and_ignores_late_old_withdrawal(self):
        new_version = self.revision(**self.original_event(18, incoming="C"))
        self.ingest(self.original_event(19, handoff_rev=new_version))
        self.assertEqual(self.statement()["result"]["totals"], {"A": 800})
        self.ingest(self.original_event(20, handoff_rev=new_version))
        accepted = self.statement()["result"]
        self.assertEqual(accepted["totals"], {"A": 150, "C": 650})
        self.ingest(self.original_event(20, decision="withdraw"))
        self.assertEqual(self.statement()["result"], accepted)
        self.ingest(self.original_event(20, handoff_rev=new_version, decision="withdraw"))
        self.assertEqual(self.statement()["result"]["totals"], {"A": 800})

    def test_competing_handoffs_block_billing_until_withdrawal(self):
        competing = self.revision(**self.original_event(18, id="parallel-offer", incoming="C"))
        self.ingest(self.original_event(19, handoff="parallel-offer", handoff_rev=competing),
                    self.original_event(20, handoff="parallel-offer", handoff_rev=competing))
        blocked = self.statement()["result"]
        self.assertEqual(blocked["blockers"], ["liability"])
        self.assertEqual(blocked["liability"]["at"], 135)
        self.assertNotIn("totals", blocked)
        self.ingest(self.original_event(20, handoff="parallel-offer", handoff_rev=competing, decision="withdraw"))
        self.assertEqual(self.statement()["result"]["totals"], {"A": 150, "B": 650})

    def test_orphan_handoff_holds_even_with_two_valid_signatures(self):
        new_version = self.revision(**self.original_event(18, outgoing="C"))
        self.ingest(self.original_event(19, handoff_rev=new_version), self.original_event(20, handoff_rev=new_version))
        result = self.statement()["result"]
        self.assertEqual(result["blockers"], ["liability"])
        self.assertEqual(result["liability"]["expected_payer"], "A")

    def test_handoff_outside_custody_or_past_tolerance_does_not_switch_payer(self):
        self.ingest(self.original_event(20, at=148))
        result = self.statement()["result"]
        self.assertEqual(result["liability"]["handoffs"][0]["status"], "signature_disagreement")
        self.ingest(self.original_event(19, at=198), self.original_event(20, at=200))
        result = self.statement()["result"]
        self.assertEqual(result["liability"]["handoffs"][0]["status"], "outside_custody")
        self.assertEqual(result["totals"], {"A": 800})
