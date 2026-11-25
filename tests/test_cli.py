from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest

from railcar.accrual import record_run
from railcar.capture import capture
from railcar.cli import FIXTURES, load, seed
from railcar.db import DomainError, connect, initialize
from railcar.settlement import approve, balance, file_invoice, prepare


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix=".ledger-test-", dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "shift.sqlite"

    def command(self, *args, code=0):
        process = subprocess.run([sys.executable, "-B", "-m", "railcar", "--db", str(self.path), *args],
                                 cwd=ROOT, text=True, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, code, process.stderr)
        if code:
            return process.stderr
        return json.loads(process.stdout)

    def test_independent_commands_and_file_backed_audit_survive_restart(self):
        self.assertIn("initialize", self.command("seed", code=2))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.command("init"), {"schema_version": 2})
        cutoff = self.command("seed")["last_rev"]
        self.assertTrue(self.command("seed")["replayed"])
        self.assertEqual(self.command("custody", "V-17", "--as-of", "210")["result"]["delivery"]["at"], 100)
        window = ("V-17", "--start", "100", "--end", "200")
        self.assertEqual(self.command("operations", *window)["result"]["incidents"][0]["status"], "approved")
        self.assertEqual(self.command("liability", *window)["result"]["spans"][1]["payer"], "B")
        self.assertEqual(self.command("tariff", "V-17", "--as-of", "100")["result"]["rate"], 10)
        self.assertEqual(self.command("evidence", str(cutoff))["event"]["kind"], "tariff")
        self.command("invoice", "fixtures/invoice.json")
        first = self.command("accrue", "V-17", "--as-of", "210")
        self.command("capture", "fixtures/late-capacity.json")
        second = self.command("accrue", "V-17", "--as-of", "210")
        self.assertTrue(self.command("audit", str(first["run"]))["matches"])
        draft = self.command("prepare", "INV-17", str(second["run"]), "writer")
        key = str(draft["adjustment"])
        self.assertIn("own adjustment", self.command("approve", key, "writer", code=2))
        self.assertEqual(self.command("adjustment", key)["status"], "pending")
        self.command("approve", key, "checker")
        self.assertEqual(self.command("balance", "INV-17")["posted"], {"B": 500})

    def test_duplicate_json_keys_do_not_silently_change_a_claim(self):
        self.command("init")
        self.command("seed")
        file = self.path.with_name("duplicate.json")
        file.write_text('{"invoice":"INV-17","visit":"V-17","lines":{"A":150,"A":1}}', encoding="utf-8")
        self.assertIn("duplicate JSON key A", self.command("invoice", str(file), code=2))
        self.assertFalse(self.command("invoice", "fixtures/invoice.json")["replayed"])

    def test_two_connections_serialize_competing_approvals(self):
        with closing(connect(self.path)) as db:
            initialize(db)
            seed(db)
            file_invoice(db, load(FIXTURES / "invoice.json"))
            capture(db, load(FIXTURES / "late-capacity.json"))
            run = record_run(db, "V-17", 210)["run"]
            proposals = [prepare(db, "INV-17", run, clerk)["adjustment"] for clerk in ("one", "two")]
        barrier = Barrier(2, timeout=5)

        def posting(adjustment):
            with closing(connect(self.path)) as db:
                barrier.wait()
                try:
                    return approve(db, adjustment, "checker")["status"]
                except DomainError as exc:
                    return str(exc)

        with ThreadPoolExecutor(2) as threads:
            outcomes = list(threads.map(posting, proposals))
        self.assertCountEqual(outcomes, ["posted", "posted balance changed: prepare a fresh adjustment"])
        with closing(connect(self.path)) as db:
            self.assertEqual(balance(db, "INV-17")["posted"], {"B": 500})
            self.assertEqual(len(balance(db, "INV-17")["entries"]), 1)
