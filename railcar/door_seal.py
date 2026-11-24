"""Read immutable cargo-door seal observations from the station ledger."""
import json

from .db import DomainError, require_text, visit_at
from .delivery import crossing


KIND = "seal_observation"


def latest(db, visit, door, cutoff):
    rows = db.execute("""
        SELECT rev, payload FROM revision
        WHERE action = ? AND rev <= ? AND json_extract(payload, '$.visit') = ?
          AND json_extract(payload, '$.door') = ?
        ORDER BY rev
    """, (KIND, cutoff, visit, door))
    active = {}
    for row in rows:
        event = json.loads(row["payload"])
        active[event["id"]] = {**event, "rev": row["rev"]}
    return sorted(active.values(), key=lambda event: (event["at"], event["id"]))


def validate(db, event, cutoff):
    visit = visit_at(db, event["visit"], cutoff)
    if not visit["begins"] <= event["at"] < visit["ends"]:
        raise DomainError("seal observation is outside the visit window")
    if (event["seal"] == "-") != (event["condition"] == "missing"):
        raise DomainError("only a missing seal uses '-' instead of its serial")
    prior = db.execute("""
        SELECT payload FROM revision WHERE action = ? AND json_extract(payload, '$.id') = ?
        ORDER BY rev DESC LIMIT 1
    """, (KIND, event["id"])).fetchone()
    if prior is not None:
        before = json.loads(prior["payload"])
        if any(before[key] != event[key] for key in ("visit", "door", "checkpoint")):
            raise DomainError("seal observation cannot move to a different visit, door or checkpoint")


def _in_scope(event, entry, departure):
    at = event["at"]
    if event["checkpoint"] == "in":
        return entry["earliest"] <= at <= entry["at"]
    if event["checkpoint"] == "out":
        return departure["earliest"] <= at <= departure["at"]
    return entry["at"] < at < departure["earliest"]


def trace(db, visit, door, cutoff, as_of):
    require_text(door, "door")
    incoming = crossing(db, visit, "in", cutoff, as_of)
    outgoing = crossing(db, visit, "out", cutoff, as_of)
    report = {"visit": visit["id"], "door": door, "cutoff": cutoff, "as_of": as_of,
              "in": incoming, "out": outgoing, "observations": []}
    observations = [item for item in latest(db, visit["id"], door, cutoff) if item["at"] <= as_of]
    if (incoming["status"] != "confirmed" or outgoing["status"] != "confirmed"
            or incoming["at"] >= outgoing["earliest"]):
        return {**report, "state": "awaiting_crossings", "observations": observations}
    states = [(event, _in_scope(event, incoming, outgoing)) for event in observations]
    accepted = [event for event, in_scope in states if in_scope]
    report["observations"] = [dict(event, in_scope=in_scope) for event, in_scope in states]
    seals = {event["seal"] for event in accepted}
    if any(event["condition"] != "intact" for event in accepted) or len(seals) > 1:
        return {**report, "state": "exception"}
    if not {"in", "out"}.issubset(event["checkpoint"] for event in accepted):
        return {**report, "state": "awaiting_observations"}
    return {**report, "state": "continuous", "seal": seals.pop()}
