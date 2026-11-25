"""Offline weighbridge intake.

Raw weigh tickets and meter-zero vouchers arrive in one offline document per
weighing round.  A conclusion is imported through the existing ``inspection``
evidence table, so the demurrage rules in ``empty_return`` keep consuming one
uniform inspection stream.

Residual mass is an interval.  Every ticket carries its own independent reading
uncertainty; each weighing also carries the zero error of its scale, certified
by a zero voucher.  A pair of re-weighs on the same scale may subtract a shared
zero offset only while one continuous voucher covers both tickets and no other
valid zeroing event lies between them.  A residual of 20 kg or less is allowed.

Read-only previews never write revisions; every generated inspection cites the
exact raw ticket rows and zero vouchers it was derived from.
"""
from .db import DomainError, current, evidence, packed, require_minute, require_text, versioned, writing
from .delivery import crossing


RESIDUAL_LIMIT_KG = 20

ZERO_FIELDS = "id device valid_from valid_to zero_min zero_max active".split()
TICKET_FIELDS = ("id visit car station scale phase at reading read_min read_max "
                 "window_from window_to active").split()
ZERO_MINUTE_FIELDS = ("valid_from", "valid_to")
TICKET_MINUTE_FIELDS = ("at", "window_from", "window_to")
ZERO_COUNT_FIELDS = ("zero_min", "zero_max")
TICKET_COUNT_FIELDS = ("reading", "read_min", "read_max")
INSPECTION_FIELDS = ("id", "visit", "at", "result")

def _check(mapping, field, predicate, message):
    if not predicate(mapping.get(field)):
        raise DomainError(message)


def _validate_zero(event):
    _check(event, "kind", lambda v: v == "zero_voucher", "unknown weighbridge evidence kind")
    if set(event) != {"kind", *ZERO_FIELDS}:
        raise DomainError("zero_voucher fields must be: " + " ".join(ZERO_FIELDS))
    require_text(event["id"], "id")
    require_text(event["device"], "device")
    for field in ZERO_MINUTE_FIELDS:
        require_minute(event[field], field)
    for field in ZERO_COUNT_FIELDS:
        if type(event[field]) is not int:
            raise DomainError(f"{field} must be an integer")
    if not event["valid_from"] < event["valid_to"]:
        raise DomainError("valid_to must be after valid_from")
    if not event["zero_min"] <= event["zero_max"]:
        raise DomainError("zero_max must be at least zero_min")
    if type(event["active"]) is not bool:
        raise DomainError("active must be a JSON boolean")


def _validate_ticket(event):
    _check(event, "kind", lambda v: v == "weigh_ticket", "unknown weighbridge evidence kind")
    if set(event) != {"kind", *TICKET_FIELDS}:
        raise DomainError("weigh_ticket fields must be: " + " ".join(TICKET_FIELDS))
    require_text(event["id"], "id")
    require_text(event["visit"], "visit")
    require_text(event["car"], "car")
    require_text(event["station"], "station")
    require_text(event["scale"], "scale")
    if event["phase"] not in ("gross", "tare"):
        raise DomainError("phase must be one of gross, tare")
    for field in TICKET_MINUTE_FIELDS:
        require_minute(event[field], field)
    for field in TICKET_COUNT_FIELDS:
        if type(event[field]) is not int or event[field] < 0:
            raise DomainError(f"{field} must be a nonnegative integer")
    if not event["read_min"] <= event["reading"] <= event["read_max"]:
        raise DomainError("reading must lie within read_min..read_max")
    if not event["window_from"] < event["window_to"]:
        raise DomainError("window_to must be after window_from")
    if type(event["active"]) is not bool:
        raise DomainError("active must be a JSON boolean")


def _validate(document):
    if not isinstance(document, dict) or set(document) != {"batch", "events"}:
        raise DomainError("weighbridge intake requires exactly batch and events")
    batch_id = require_text(document["batch"], "batch")
    events = document["events"]
    if not isinstance(events, list) or not events:
        raise DomainError("events must be a nonempty array")
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            raise DomainError("each event must be an object")
        kind = event.get("kind")
        if kind == "zero_voucher":
            _validate_zero(event)
        elif kind == "weigh_ticket":
            _validate_ticket(event)
        else:
            raise DomainError("unknown weighbridge evidence kind")
        key = (kind, event["id"])
        if key in seen:
            raise DomainError(f"duplicate {kind} id {event['id']} in batch")
        seen.add(key)
    return batch_id, events


def _store(db, event):
    kind = event["kind"]
    if kind == "zero_voucher":
        fields = ZERO_FIELDS
    elif kind == "weigh_ticket":
        fields = TICKET_FIELDS
    else:  # imported inspection, stored through the existing evidence table
        fields = INSPECTION_FIELDS
    values = [event[field] for field in fields]
    rev = db.execute("INSERT INTO revision(action, payload) VALUES (?, ?)",
                     (kind, packed(event))).lastrowid
    columns = ", ".join([*fields, "rev"])
    placeholders = ", ".join("?" for _ in range(len(fields) + 1))
    db.execute(f"INSERT INTO {kind} ({columns}) VALUES ({placeholders})", (*values, rev))
    return rev


# --------------------------------------------------------------------------- #
# Read-only evaluation
# --------------------------------------------------------------------------- #

def _active_versions(db, table, cutoff):
    return [item for item in versioned(db, table, cutoff) if item["active"]]


def _covering_vouchers(db, scale, minute, cutoff):
    return [v for v in _active_versions(db, "zero_voucher", cutoff)
            if v["device"] == scale and v["valid_from"] <= minute < v["valid_to"]]


def _ticket_status(db, ticket, visit, cutoff, as_of):
    """Validate one raw ticket against its visit and weighing window."""
    refs = {"ticket": evidence(ticket)}
    if ticket["visit"] != visit["id"]:
        return {**refs, "status": "different_visit"}
    if ticket["car"] != visit["car"]:
        # A ticket from another railcar can never stand in for this visit.
        return {**refs, "status": "different_car"}
    if ticket["station"] != visit["station"]:
        return {**refs, "status": "different_station"}
    if not visit["begins"] <= ticket["at"] < visit["ends"]:
        return {**refs, "status": "outside_visit_window"}
    if ticket["at"] > as_of:
        return {**refs, "status": "after_as_of"}
    if not ticket["window_from"] <= ticket["at"] < ticket["window_to"]:
        return {**refs, "status": "outside_weighing_window"}
    vouchers = _covering_vouchers(db, ticket["scale"], ticket["at"], cutoff)
    refs["zero_vouchers"] = [list(evidence(v)) for v in vouchers]
    if len(vouchers) != 1:
        return {**refs, "status": "zero_missing" if not vouchers else "zero_conflict"}
    return {**refs, "status": "eligible", "voucher": vouchers[0]}


def _paired_zero(gross_ticket, tare_ticket, gross_detail, tare_detail, all_vouchers):
    """Return the shared continuous zero voucher, or None when pairing fails."""
    if gross_ticket["scale"] != tare_ticket["scale"]:
        return None
    gv, tv = gross_detail["voucher"], tare_detail["voucher"]
    if gv["id"] != tv["id"]:
        return None
    first, second = sorted((gross_ticket, tare_ticket), key=lambda item: item["at"])
    # Any other active zeroing event on this scale whose validity starts
    # between the two readings breaks continuity, even if it never covered a
    # weighing itself.
    for voucher in all_vouchers:
        if voucher["device"] != gross_ticket["scale"] or voucher["id"] == gv["id"]:
            continue
        if first["at"] < voucher["valid_from"] <= second["at"]:
            return None
    return gv


def _residual(gross_ticket, tare_ticket, gross_detail, tare_detail, all_vouchers):
    """Closed residual interval (low, high) plus the zero evidence that applies."""
    shared = _paired_zero(gross_ticket, tare_ticket, gross_detail, tare_detail, all_vouchers)
    if shared is not None:
        # One continuous zero certificate covers both tickets: the shared zero
        # offset cancels in the gross-minus-tare difference, so only the two
        # independent read errors widen the residual.
        zero_low, zero_high = 0, 0
        zero_evidence = [list(evidence(shared))]
        mode = "paired_shared_zero"
    else:
        # Independent weighings: each ticket keeps its own scale's zero error,
        # so the two zero bands add into the gross-minus-tare difference.
        gv, tv = gross_detail["voucher"], tare_detail["voucher"]
        zero_low = gv["zero_min"] - tv["zero_max"]
        zero_high = gv["zero_max"] - tv["zero_min"]
        zero_evidence = sorted({tuple(evidence(gv)), tuple(evidence(tv))})
        mode = "independent_zero"
    low = gross_ticket["read_min"] - tare_ticket["read_max"] + zero_low
    high = gross_ticket["read_max"] - tare_ticket["read_min"] + zero_high
    return {"low": low, "high": high, "mode": mode,
            "zero_evidence": [list(ref) for ref in zero_evidence]}


def _verdict(residual):
    if residual["high"] <= RESIDUAL_LIMIT_KG:
        return "ready"
    if residual["low"] > RESIDUAL_LIMIT_KG:
        return "rejected"
    return "inconclusive"


def evaluate_visit(db, visit_id, cutoff=None, as_of=None):
    """Read-only weighing preview for one visit; never writes anything.

    Uses the caller's open transaction, or an implicit read-only connection;
    this function itself issues no COMMIT/ROLLBACK.
    """
    cutoff = current(db) if cutoff is None else cutoff
    visit = db.execute("SELECT * FROM visit WHERE id = ? AND rev <= ? "
                       "ORDER BY rev DESC LIMIT 1", (visit_id, cutoff)).fetchone()
    if visit is None:
        raise DomainError(f"visit {visit_id} is unavailable at revision {cutoff}")
    if as_of is None:
        as_of = visit["ends"] - 1
    require_minute(as_of, "as_of")
    # Keep every active ticket of the visit; _ticket_status explains why any
    # out-of-window or future ticket cannot serve as evidence.
    tickets = [item for item in _active_versions(db, "weigh_ticket", cutoff)
               if item["visit"] == visit_id]
    details = [_ticket_status(db, ticket, visit, cutoff, as_of) for ticket in tickets]
    all_vouchers = _active_versions(db, "zero_voucher", cutoff)
    eligible = [detail for detail in details if detail["status"] == "eligible"]
    by_rev = {item["rev"]: item for item in tickets}
    phases = {}
    for detail in eligible:
        ticket = by_rev[detail["ticket"][1]]
        phases.setdefault(ticket["phase"], []).append((ticket, detail))
    result = {"visit": visit_id, "cutoff": cutoff, "as_of": as_of,
              "tickets": [{key: value for key, value in detail.items() if key != "voucher"}
                          for detail in details],
              "residual_limit_kg": RESIDUAL_LIMIT_KG}
    competing = sorted(phase for phase, items in phases.items() if len(items) > 1)
    if competing:
        return {**result, "status": "competing_tickets", "phases": competing}
    if not eligible:
        return {**result, "status": "evidence_missing"}
    if "gross" not in phases or "tare" not in phases:
        return {**result, "status": "evidence_missing",
                "missing_phase": "tare" if "gross" in phases else "gross"}
    gross_ticket, gross_detail = phases["gross"][0]
    tare_ticket, tare_detail = phases["tare"][0]
    # The loaded (gross) weighing must happen before the empty (tare) one.
    if gross_ticket["at"] >= tare_ticket["at"]:
        return {**result, "status": "tare_before_gross"}
    residual = _residual(gross_ticket, tare_ticket, gross_detail, tare_detail, all_vouchers)
    verdict = _verdict(residual)
    evidence_chain = sorted({
        *[tuple(ref) for ref in gross_detail["zero_vouchers"]],
        *[tuple(ref) for ref in tare_detail["zero_vouchers"]],
        tuple(evidence(gross_ticket)), tuple(evidence(tare_ticket)),
    })
    result.update({"status": verdict, "residual_kg": [residual["low"], residual["high"]],
                   "zero_mode": residual["mode"],
                   "zero_evidence": residual["zero_evidence"],
                   "weighing": {"gross": evidence(gross_ticket), "tare": evidence(tare_ticket),
                                "at": tare_ticket["at"]},
                   "evidence_chain": [list(ref) for ref in evidence_chain]})
    if verdict == "inconclusive":
        result["reason"] = "residual_interval_crosses_limit"
    return result


# --------------------------------------------------------------------------- #
# Offline intake with import into the existing inspection stream
# --------------------------------------------------------------------------- #

def _resolve_verdict(db, visit_id, cutoff, as_of, departure_earliest, inspection_id):
    """The verdict that may be imported as an inspection, plus its evidence."""
    preview = evaluate_visit(db, visit_id, cutoff, as_of)
    if preview["status"] not in ("ready", "rejected"):
        return preview, None
    # A ticket at or past the earliest exit reading cannot make the car empty:
    # the departure boundary had already happened when it was weighed.
    weighing_at = preview["weighing"]["at"]
    if departure_earliest is not None and weighing_at >= departure_earliest:
        return {**preview, "status": "after_departure",
                "reason": "weighing_at_or_after_earliest_exit_reading"}, None
    return preview, {
        "id": inspection_id,
        "visit": visit_id,
        "at": weighing_at,
        "result": preview["status"],
        "evidence_chain": preview["evidence_chain"],
        "residual_kg": preview["residual_kg"],
        "zero_mode": preview["zero_mode"],
    }


def _validate_visit(db, events, cutoff):
    visits = sorted({event["visit"] for event in events if event["kind"] == "weigh_ticket"})
    if len(visits) != 1:
        raise DomainError("a weighbridge batch must cover exactly one visit")
    visit_id = visits[0]
    visit = db.execute("SELECT * FROM visit WHERE id = ? AND rev <= ? ORDER BY rev DESC LIMIT 1",
                       (visit_id, cutoff)).fetchone()
    if visit is None:
        raise DomainError(f"visit {visit_id} is unavailable at revision {cutoff}")
    return visit_id, visit


def intake(db, document, as_of=None, preview=False):
    """Ingest an offline weighbridge file and, when decidable, import evidence.

    With ``preview=True`` nothing is committed: the raw rows are staged inside
    one rolled-back transaction so the verdict reflects exactly this file.
    """
    batch_id, events = _validate(document)

    def stage():
        previous = db.execute("SELECT * FROM weigh_batch WHERE id = ?", (batch_id,)).fetchone()
        payload = packed(events)
        if previous:
            if previous["payload"] != payload:
                raise DomainError(f"weighbridge batch {batch_id} already has a different payload")
            revs = [row["rev"] for row in db.execute(
                "SELECT rev FROM revision WHERE rev BETWEEN ? AND ? ORDER BY rev",
                (previous["first_rev"], previous["last_rev"]))]
            inspection = None
            if previous["inspection_rev"] is not None:
                row = db.execute("SELECT * FROM inspection WHERE rev = ?",
                                  (previous["inspection_rev"],)).fetchone()
                inspection = {"inspection": evidence(row), "result": row["result"], "at": row["at"]}
            visit_id, _ = _validate_visit(db, events, previous["last_rev"])
            return {"batch": batch_id, "first_rev": previous["first_rev"],
                    "last_rev": previous["last_rev"], "replayed": True, "revisions": revs,
                    "result": evaluate_visit(db, visit_id, previous["last_rev"]),
                    "inspection": inspection}
        first = current(db) + 1
        revs = [_store(db, event) for event in events]
        last = current(db)
        visit_id, visit = _validate_visit(db, events, last)
        effective_as_of = visit["ends"] - 1 if as_of is None else require_minute(as_of, "as_of")
        departure = crossing(db, visit, "out", last, effective_as_of)
        earliest = departure.get("earliest") if departure["status"] == "confirmed" else None
        inspection_id = f"weigh-{batch_id}"
        preview_result, imported = _resolve_verdict(
            db, visit_id, last, effective_as_of, earliest, inspection_id)
        inspection_info = None
        inspection_rev = None
        if imported is not None:
            event = {"kind": "inspection", "id": imported["id"], "visit": imported["visit"],
                     "at": imported["at"], "result": imported["result"]}
            inspection_rev = _store(db, event)
            inspection_info = {"inspection": [event["id"], inspection_rev],
                               "result": event["result"], "at": event["at"],
                               "evidence_chain": imported["evidence_chain"],
                               "residual_kg": imported["residual_kg"],
                               "zero_mode": imported["zero_mode"]}
        return {"batch": batch_id, "first_rev": first, "last_rev": last, "replayed": False,
                "revisions": revs, "result": preview_result, "inspection": inspection_info,
                "payload": payload, "inspection_rev": inspection_rev}

    if preview:
        db.execute("BEGIN IMMEDIATE")
        try:
            staged = stage()
        finally:
            db.rollback()
        if staged["replayed"]:
            # A replayed batch has nothing new to stage; show the stored verdict.
            return {key: value for key, value in staged.items()
                    if key not in ("payload", "inspection_rev")}
        return {"batch": batch_id, "preview": True,
                "would_write_revisions": [staged["first_rev"], staged["last_rev"]],
                "result": staged["result"], "inspection": staged["inspection"]}

    with writing(db):
        staged = stage()
        if staged["replayed"]:
            return {key: value for key, value in staged.items()
                    if key not in ("payload", "inspection_rev")}
        db.execute("INSERT INTO weigh_batch VALUES (?, ?, ?, ?, ?)",
                   (batch_id, staged["payload"], staged["first_rev"], staged["last_rev"],
                    staged["inspection_rev"]))
        return {key: value for key, value in staged.items()
                if key not in ("payload", "inspection_rev")}
