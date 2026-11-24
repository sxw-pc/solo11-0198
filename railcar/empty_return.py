"""An empty-car inspection makes it tenderable. Physical exit ends custody."""
from .db import evidence, versioned
from .delivery import crossing


def empty_return(db, visit, cutoff, as_of, delivery):
    departure = crossing(db, visit, "out", cutoff, as_of)
    lower = delivery.get("at", visit["begins"])
    upper = departure.get("earliest", min(as_of, visit["ends"] - 1))
    inspections = [item for item in versioned(db, "inspection", cutoff, visit=visit["id"])
                   if lower <= item["at"] <= upper]
    last = max((item["at"] for item in inspections), default=None)
    governing = [item for item in inspections if item["at"] == last]
    decisions = {item["result"] for item in governing}
    detail = {"crossing": departure, "inspections": [evidence(item) for item in governing]}
    if delivery["status"] != "confirmed":
        return {**detail, "status": "awaiting_delivery"}
    if departure["status"] == "confirmed" and departure["earliest"] <= delivery["at"]:
        return {**detail, "status": "departure_not_after_delivery"}
    if len(decisions) > 1:
        return {**detail, "status": "inspection_conflict"}
    if not decisions:
        return {**detail, "status": "inspection_missing"}
    if decisions == {"rejected"}:
        return {**detail, "status": "rejected"}
    if departure["status"] != "confirmed":
        return {**detail, "status": "tendered", "tender_at": last}
    return {**detail, "status": "returned", "tender_at": last, "at": departure["at"]}
