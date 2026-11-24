"""Resolve version-bound interline handoffs into a single liability chain."""
from itertools import groupby

from .db import evidence, versioned


SIGN_TOLERANCE = 15


def _handoff(db, item, cutoff, start, end):
    signs = versioned(db, "signature", cutoff, key=("handoff", "handoff_rev", "side"),
                       handoff=item["id"], handoff_rev=item["rev"])
    result = {"evidence": evidence(item), "outgoing": item["outgoing"], "incoming": item["incoming"],
              "signatures": [[sign["side"], sign["rev"]] for sign in signs]}
    if any(sign["decision"] == "withdraw" for sign in signs):
        return {**result, "status": "withdrawn"}
    if {sign["side"] for sign in signs if sign["decision"] == "accept"} != {"outgoing", "incoming"}:
        return {**result, "status": "awaiting_both"}
    early, late = sorted(sign["at"] for sign in signs)
    result["at"] = late
    if late >= end or early < start:
        return {**result, "status": "outside_custody"}
    if late - early > SIGN_TOLERANCE:
        return {**result, "status": "signature_disagreement"}
    return {**result, "status": "accepted"}


def liability(db, visit, cutoff, start, end):
    details = [_handoff(db, item, cutoff, start, end)
               for item in versioned(db, "handoff", cutoff, visit=visit["id"])]
    candidates = sorted((item for item in details if item["status"] == "accepted"), key=lambda item: item["at"])
    party, left = visit["initial_payer"], start
    spans = []
    for minute, simultaneous in groupby(candidates, key=lambda item: item["at"]):
        changes = list(simultaneous)
        if len(changes) != 1 or changes[0]["outgoing"] != party:
            return {"status": "blocked", "at": minute, "expected_payer": party,
                    "conflicts": [item["evidence"] for item in changes], "handoffs": details, "spans": []}
        if left < minute:
            spans.append({"from": left, "to": minute, "payer": party})
        party, left = changes[0]["incoming"], minute
    if left < end:
        spans.append({"from": left, "to": end, "payer": party})
    return {"status": "allocated", "handoffs": details, "spans": spans}
