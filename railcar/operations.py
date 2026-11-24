"""Adjudicate interruption eligibility and certified station capacity."""
from itertools import pairwise

from .db import evidence, versioned


def _incident(db, item, cutoff):
    attestations = versioned(db, "attestation", cutoff, key=("incident", "incident_rev", "side"),
                             incident=item["id"], incident_rev=item["rev"])
    # Older-version signatures do not carry forward to a corrected report.
    signs = {sign["side"]: sign["decision"] for sign in attestations}
    if item["cause"] != "equipment":
        status = "ineligible_cause"
    elif "deny" in signs.values():
        status = "denied"
    elif set(signs) == {"carrier", "yard"}:
        status = "approved"
    else:
        status = "awaiting_both"
    return {"evidence": evidence(item), "status": status, "from": item["begins"], "to": item["ends"],
            "attestations": [[sign["side"], sign["rev"]] for sign in attestations]}


def assess_operations(db, visit, cutoff, start, end):
    incidents = [_incident(db, item, cutoff) for item in versioned(db, "incident", cutoff, visit=visit["id"])
                 if item["begins"] < end and start < item["ends"]]
    capacities = [item for item in versioned(db, "capacity", cutoff, station=visit["station"])
                  if item["begins"] < end and start < item["ends"]]
    boundaries = {start, end}
    for item in incidents:
        boundaries.update((max(start, item["from"]), min(end, item["to"])))
    for item in capacities:
        boundaries.update((max(start, item["begins"]), min(end, item["ends"])))
    segments = []
    for left, right in pairwise(sorted(boundaries)):
        windows = [item for item in capacities if item["begins"] <= left < item["ends"]]
        slots = {window["slots"] for window in windows}
        if not slots:
            capacity = "unknown"
        elif len(slots) > 1:
            capacity = "conflict"
        else:
            capacity = "zero" if slots == {0} else "available"
        approved = [item["evidence"] for item in incidents
                    if item["status"] == "approved" and item["from"] <= left < item["to"]]
        segments.append({"from": left, "to": right, "capacity": capacity,
                         "certificates": [evidence(item) for item in windows],
                         "approved_incidents": approved, "paused": capacity == "zero" and bool(approved)})
    return {"incidents": incidents, "segments": segments}
