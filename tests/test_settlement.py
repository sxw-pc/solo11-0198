from railcar.accrual import audit_run
from railcar.db import DomainError
from railcar.settlement import approve, balance, file_invoice, inspect_adjustment, prepare
from support import FieldLedger


class SettlementTests(FieldLedger):
    def test_invoice_replay_preserves_immutable_original_lines(self):
        self.assertFalse(self.invoice()["replayed"])
        self.assertTrue(self.invoice()["replayed"])
        with self.assertRaisesRegex(DomainError, "different claim"):
            file_invoice(self.db, {"invoice": "INV-17", "visit": "V-17", "lines": {"A": 150}})
        self.assertEqual(balance(self.db, "INV-17")["original"], {"A": 150, "B": 650})
        self.assertEqual(prepare(self.db, "INV-17", self.statement()["run"], "writer")["status"], "balanced")

    def test_versioned_handoff_creates_credit_and_rebill_despite_unchanged_total(self):
        self.invoice()
        old = self.statement()
        version = self.revision(**self.original_event(18, incoming="C"))
        self.ingest(self.original_event(19, handoff_rev=version), self.original_event(20, handoff_rev=version))
        latest = self.statement()
        self.assertEqual(sum(old["result"]["totals"].values()), sum(latest["result"]["totals"].values()))
        proposal = prepare(self.db, "INV-17", latest["run"], "writer")
        self.assertEqual(proposal["deltas"], {"B": -650, "C": 650})
        self.assertEqual(inspect_adjustment(self.db, proposal["adjustment"])["status"], "pending")
        approve(self.db, proposal["adjustment"], "checker")
        audit = inspect_adjustment(self.db, proposal["adjustment"])
        self.assertEqual((audit["status"], audit["approved_by"], audit["baseline"]), ("posted", "checker", []))
        self.assertEqual(balance(self.db, "INV-17")["posted"], {"A": 150, "C": 650})
        self.assertTrue(audit_run(self.db, old["run"])["matches"])

    def test_finality_requires_ready_and_exit_prior_to_reconciliation(self):
        self.invoice()
        self.ingest(self.original_event(11, result="rejected"))
        provisional = self.statement()
        with self.assertRaisesRegex(DomainError, "only a final run"):
            prepare(self.db, "INV-17", provisional["run"], "writer")
        self.ingest(self.original_event(11))
        self.assertEqual(prepare(self.db, "INV-17", self.statement()["run"], "writer")["status"], "balanced")

    def test_changed_capacity_invalidates_pending_true_up_and_allows_fresh_proposal(self):
        self.invoice()
        self.ingest(self.original_event(17, slots=0))
        old = self.statement()
        proposal = prepare(self.db, "INV-17", old["run"], "writer")
        self.ingest(self.original_event(17, ends=140, slots=0))
        with self.assertRaisesRegex(DomainError, "evidence changed"):
            approve(self.db, proposal["adjustment"], "checker")
        self.assertEqual(inspect_adjustment(self.db, proposal["adjustment"])["status"], "stale_evidence")
        self.assertEqual(balance(self.db, "INV-17")["entries"], [])
        fresh = prepare(self.db, "INV-17", self.statement()["run"], "writer")
        self.assertEqual(fresh["deltas"], {"A": -150, "B": -50})
        approve(self.db, fresh["adjustment"], "checker")
        self.assertEqual(balance(self.db, "INV-17")["posted"], {"B": 600})
        self.assertTrue(audit_run(self.db, old["run"])["matches"])

    def test_separation_of_duties_and_competing_proposals_prevent_double_post(self):
        self.invoice()
        self.ingest(self.original_event(17, slots=0))
        run = self.statement()["run"]
        first = prepare(self.db, "INV-17", run, "one")["adjustment"]
        self.assertEqual(prepare(self.db, "INV-17", run, "one")["adjustment"], first)
        second = prepare(self.db, "INV-17", run, "two")["adjustment"]
        with self.assertRaisesRegex(DomainError, "own adjustment"):
            approve(self.db, first, "one")
        self.assertFalse(approve(self.db, first, "checker")["replayed"])
        self.assertTrue(approve(self.db, first, "checker")["replayed"])
        with self.assertRaisesRegex(DomainError, "posted balance changed"):
            approve(self.db, second, "checker")
        self.assertEqual(inspect_adjustment(self.db, second)["status"], "stale_balance")
        self.assertEqual(prepare(self.db, "INV-17", run, "one")["status"], "balanced")
        self.assertEqual(balance(self.db, "INV-17")["posted"], {"B": 500})
        self.assertEqual(len(balance(self.db, "INV-17")["entries"]), 1)

    def test_second_approved_true_up_compares_with_posted_balance(self):
        self.invoice()
        self.ingest(self.original_event(17, slots=0))
        first = prepare(self.db, "INV-17", self.statement()["run"], "writer")
        approve(self.db, first["adjustment"], "checker")
        self.ingest(self.original_event(17, slots=1))
        second = prepare(self.db, "INV-17", self.statement()["run"], "writer")
        self.assertEqual(second["deltas"], {"A": 150, "B": 150})
        self.assertEqual(inspect_adjustment(self.db, second["adjustment"])["baseline"], [first["adjustment"]])
        self.ingest({"kind": "car", "id": "UNRELATED"})
        approve(self.db, second["adjustment"], "checker")
        final = balance(self.db, "INV-17")
        self.assertEqual(final["posted"], final["original"])
        self.assertEqual(len(final["entries"]), 2)
