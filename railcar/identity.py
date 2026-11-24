"""Resolve raw device time and rolling-stock marks independently of billing."""
from .db import evidence, versioned


def observation_identity(db, observation, visit, cutoff, as_of):
    detail = {"observation": evidence(observation), "side": observation["side"], "device": observation["device"]}
    if not observation["active"]:
        return {**detail, "status": "withdrawn"}
    if observation["gate"] != visit["gate"]:
        return {**detail, "status": "wrong_gate"}
    raw = observation["raw"]
    clocks = [clock for clock in versioned(db, "clock", cutoff, device=observation["device"])
              if clock["raw_from"] <= raw < clock["raw_to"]]
    detail["clocks"] = [evidence(clock) for clock in clocks]
    if len(clocks) != 1:
        return {**detail, "status": "clock_missing" if not clocks else "clock_conflict"}
    minute = raw + clocks[0]["offset"]
    detail["minute"] = minute
    if not visit["begins"] <= minute < visit["ends"]:
        return {**detail, "status": "outside_visit_window"}
    if minute > as_of:
        return {**detail, "status": "after_as_of"}
    marks = [mark for mark in versioned(db, "mark", cutoff, label=observation["label"])
             if mark["active"] and mark["begins"] <= minute < mark["ends"]]
    detail["marks"] = [evidence(mark) for mark in marks]
    cars = sorted({mark["car"] for mark in marks})
    if len(cars) != 1:
        return {**detail, "status": "mark_missing" if not cars else "mark_conflict", "cars": cars}
    return {**detail, "status": "eligible" if cars[0] == visit["car"] else "different_car", "car": cars[0]}
