"""Decide whether both sides witnessed the same passage at the contracted gate."""
from .db import versioned
from .identity import observation_identity


PAIR_TOLERANCE = 10


def crossing(db, visit, phase, cutoff, as_of):
    observations = versioned(db, "observation", cutoff, visit=visit["id"], phase=phase)
    details = [observation_identity(db, item, visit, cutoff, as_of) for item in observations]
    candidates = {side: [item for item in details if item["side"] == side and item["status"] == "eligible"]
                  for side in ("carrier", "yard")}
    result = {"observations": details}
    if any(len(items) > 1 for items in candidates.values()):
        return {**result, "status": "competing_observations"}
    if any(not items for items in candidates.values()):
        return {**result, "status": "unproven"}
    carrier, yard = (candidates[side][0] for side in ("carrier", "yard"))
    if carrier["device"] == yard["device"]:
        return {**result, "status": "shared_device"}
    early, late = sorted((carrier["minute"], yard["minute"]))
    if late - early > PAIR_TOLERANCE:
        return {**result, "status": "clock_disagreement"}
    return {**result, "status": "confirmed", "at": late, "earliest": early}
