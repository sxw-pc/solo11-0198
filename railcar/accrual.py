"""Compose custody, work stoppages and liability into a reproducible statement."""
import json

from .db import DomainError, evidence, packed, reading, require_minute, row, snapshot, visit_at, writing
from .delivery import crossing
from .empty_return import empty_return
from .liability import liability
from .operations import assess_operations
from .pricing import meter, tariff_at


def decisions(db, visit, cutoff, as_of):
    delivery = crossing(db, visit, "in", cutoff, as_of)
    returned = empty_return(db, visit, cutoff, as_of, delivery)
    return {"visit_evidence": evidence(visit), "delivery": delivery, "return": returned}


def evaluate(db, visit_id, cutoff, as_of):
    require_minute(as_of, "as_of")
    visit = visit_at(db, visit_id, cutoff)
    if as_of < visit["begins"]:
        raise DomainError("as_of precedes the visit window")
    result = decisions(db, visit, cutoff, as_of)
    if result["delivery"]["status"] != "confirmed":
        return {**result, "status": "held", "blockers": ["delivery"]}
    start = result["delivery"]["at"]
    end = result["return"].get("at", min(as_of, visit["ends"]))
    tariff = tariff_at(db, visit, cutoff, start)
    responsibility = liability(db, visit, cutoff, start, end)
    operations = assess_operations(db, visit, cutoff, start, end)
    result.update({"custody": [start, end], "tariff": tariff, "liability": responsibility, "operations": operations})
    blockers = []
    if tariff["status"] != "applied":
        blockers.append("tariff")
    if responsibility["status"] != "allocated":
        blockers.append("liability")
    if blockers:
        return {**result, "status": "held", "blockers": blockers}
    status = "final" if result["return"]["status"] == "returned" else "provisional"
    return {**result, "status": status, **meter(operations["segments"], responsibility["spans"], tariff)}


def record_run(db, visit_id, as_of, cutoff=None):
    with writing(db):
        cutoff = snapshot(db, cutoff)
        result = evaluate(db, visit_id, cutoff, as_of)
        db.execute("INSERT OR IGNORE INTO run(visit, cutoff, as_of, result) VALUES (?, ?, ?, ?)",
                   (visit_id, cutoff, as_of, packed(result)))
        run = db.execute("SELECT * FROM run WHERE visit = ? AND cutoff = ? AND as_of = ?",
                         (visit_id, cutoff, as_of)).fetchone()
        if run["result"] != packed(result):
            raise DomainError("stored run disagrees with this rule implementation")
        return {"run": run["id"], "visit": visit_id, "cutoff": cutoff, "as_of": as_of, "result": result}


def load_run(db, run_id):
    return row(db, "SELECT * FROM run WHERE id = ?", (run_id,), f"run {run_id} does not exist")


def audit_run(db, run_id):
    with reading(db):
        run = load_run(db, run_id)
        recalculated = evaluate(db, run["visit"], run["cutoff"], run["as_of"])
        return {"run": run_id, "visit": run["visit"], "cutoff": run["cutoff"], "as_of": run["as_of"],
                "matches": packed(recalculated) == run["result"], "stored": json.loads(run["result"]),
                "recalculated": recalculated}
