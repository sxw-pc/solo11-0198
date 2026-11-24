"""Reconcile original carrier bill plus approved true-ups against a final run."""
import json

from .accrual import evaluate, load_run
from .db import DomainError, current, packed, require_text, row, writing


def file_invoice(db, document):
    if not isinstance(document, dict) or set(document) != {"invoice", "visit", "lines"}:
        raise DomainError("invoice requires exactly invoice, visit and lines")
    invoice = require_text(document["invoice"], "invoice")
    visit = require_text(document["visit"], "visit")
    lines = document["lines"]
    if not isinstance(lines, dict):
        raise DomainError("invoice lines must be an object keyed by payer")
    for payer, cents in lines.items():
        require_text(payer, "payer")
        if type(cents) is not int or not 0 < cents <= 10**17:
            raise DomainError("invoice amounts must be positive integer cents up to 10^17")
    with writing(db):
        previous = db.execute("SELECT visit FROM invoice WHERE id = ?", (invoice,)).fetchone()
        if previous:
            if visit != previous["visit"] or lines != _original(db, invoice):
                raise DomainError("invoice reference already has a different claim")
            return {"invoice": invoice, "replayed": True}
        db.execute("INSERT INTO invoice VALUES (?, ?)", (invoice, visit))
        db.executemany("INSERT INTO invoice_line VALUES (?, ?, ?)",
                       [(invoice, payer, cents) for payer, cents in lines.items()])
    return {"invoice": invoice, "replayed": False}


def _original(db, invoice):
    return {item["payer"]: item["cents"] for item in db.execute(
        "SELECT payer, cents FROM invoice_line WHERE invoice = ? ORDER BY payer", (invoice,))}


def balance(db, invoice):
    bill = row(db, "SELECT * FROM invoice WHERE id = ?", (invoice,), f"invoice {invoice} does not exist")
    original = _original(db, invoice)
    posted = original.copy()
    entries = []
    for adjustment in db.execute("""SELECT adjustment.*, approval.approved_by FROM adjustment
         JOIN approval ON approval.adjustment = adjustment.id WHERE invoice = ? ORDER BY adjustment.id""", (invoice,)):
        deltas = json.loads(adjustment["deltas"])
        for payer, cents in deltas.items():
            posted[payer] = posted.get(payer, 0) + cents
        entries.append({"adjustment": adjustment["id"], "run": adjustment["run"], "deltas": deltas,
                        "prepared_by": adjustment["prepared_by"], "approved_by": adjustment["approved_by"]})
    return {"invoice": invoice, "visit": bill["visit"], "original": original,
            "posted": {payer: cents for payer, cents in posted.items() if cents}, "entries": entries}


def _current_final(db, run, visit):
    if run["visit"] != visit:
        raise DomainError("run and invoice belong to different visits")
    result = json.loads(run["result"])
    if result["status"] != "final":
        raise DomainError("only a final run can authorize a true-up")
    refreshed = evaluate(db, visit, current(db), run["as_of"])
    if packed(refreshed) != run["result"]:
        raise DomainError("evidence changed: record a current run before true-up")
    return result["totals"]


def prepare(db, invoice, run_id, operator):
    require_text(operator, "operator")
    with writing(db):
        ledger = balance(db, invoice)
        target = _current_final(db, load_run(db, run_id), ledger["visit"])
        posted = ledger["posted"]
        deltas = {payer: target.get(payer, 0) - posted.get(payer, 0) for payer in sorted(target.keys() | posted.keys())
                  if target.get(payer, 0) != posted.get(payer, 0)}
        if not deltas:
            return {"status": "balanced", "invoice": invoice, "deltas": {}}
        baseline = [entry["adjustment"] for entry in ledger["entries"]]
        old = db.execute("""SELECT id FROM adjustment WHERE invoice = ? AND run = ? AND prepared_by = ?
                            AND baseline = ?""", (invoice, run_id, operator, packed(baseline))).fetchone()
        adjustment = old["id"] if old else db.execute("""INSERT INTO adjustment
            (invoice, run, prepared_by, target, baseline, deltas) VALUES (?, ?, ?, ?, ?, ?)""",
            (invoice, run_id, operator, packed(target), packed(baseline), packed(deltas))).lastrowid
        return {"status": "proposed", "adjustment": adjustment, "deltas": deltas}


def approve(db, adjustment_id, operator):
    require_text(operator, "operator")
    with writing(db):
        proposal = row(db, "SELECT * FROM adjustment WHERE id = ?", (adjustment_id,), "adjustment does not exist")
        previous = db.execute("SELECT approved_by FROM approval WHERE adjustment = ?", (adjustment_id,)).fetchone()
        if previous:
            if previous["approved_by"] != operator:
                raise DomainError("adjustment was already approved by another operator")
            return {"status": "posted", "adjustment": adjustment_id, "replayed": True}
        if operator == proposal["prepared_by"]:
            raise DomainError("preparer cannot approve their own adjustment")
        ledger = balance(db, proposal["invoice"])
        _current_final(db, load_run(db, proposal["run"]), ledger["visit"])
        if packed([entry["adjustment"] for entry in ledger["entries"]]) != proposal["baseline"]:
            raise DomainError("posted balance changed: prepare a fresh adjustment")
        db.execute("INSERT INTO approval VALUES (?, ?)", (adjustment_id, operator))
        return {"status": "posted", "adjustment": adjustment_id, "replayed": False}


def inspect_adjustment(db, adjustment_id):
    item = row(db, "SELECT * FROM adjustment WHERE id = ?", (adjustment_id,), "adjustment does not exist")
    ledger = balance(db, item["invoice"])
    run = load_run(db, item["run"])
    posted = next((entry for entry in ledger["entries"] if entry["adjustment"] == adjustment_id), None)
    result = {"adjustment": adjustment_id, "invoice": item["invoice"], "run": item["run"],
              "cutoff": run["cutoff"], "as_of": run["as_of"], "target": json.loads(item["target"]),
              "baseline": json.loads(item["baseline"]), "deltas": json.loads(item["deltas"]),
              "prepared_by": item["prepared_by"]}
    if posted:
        return {**result, "status": "posted", "approved_by": posted["approved_by"]}
    try:
        _current_final(db, run, ledger["visit"])
    except DomainError as exc:
        return {**result, "status": "stale_evidence", "reason": str(exc)}
    if packed([entry["adjustment"] for entry in ledger["entries"]]) != item["baseline"]:
        return {**result, "status": "stale_balance"}
    return {**result, "status": "pending"}
