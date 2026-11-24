"""Append evidence through one transaction per offline capture file."""
from .db import DomainError, current, packed, require_minute, require_text, writing


# Scope keys cannot be moved when a field record is corrected.
LAYOUT = {
    "car": ("id", ""),
    "visit": ("id car station gate initial_payer begins ends", ""),
    "mark": ("id label car begins ends active", ""),
    "clock": ("id device raw_from raw_to offset", "device"),
    "observation": ("id visit phase side device raw label gate active", "visit phase side"),
    "inspection": ("id visit at result", "visit"),
    "cut_link": ("id cut first_car first_end second_car second_end at state", "cut first_car first_end second_car second_end"),
    "incident": ("id visit begins ends cause", "visit"),
    "attestation": ("incident incident_rev side decision", ""),
    "capacity": ("id station begins ends slots", "station"),
    "handoff": ("id visit outgoing incoming", "visit"),
    "signature": ("handoff handoff_rev side at decision", ""),
    "tariff": ("id station begins ends free rate", "station"),
}
CHOICES = {
    "observation": {"phase": ("in", "out"), "side": ("carrier", "yard")},
    "inspection": {"result": ("ready", "rejected")},
    "cut_link": {"first_end": ("A", "B"), "second_end": ("A", "B"), "state": ("latched", "open")},
    "incident": {"cause": ("equipment", "carrier", "weather")},
    "attestation": {"side": ("carrier", "yard"), "decision": ("approve", "deny")},
    "signature": {"side": ("outgoing", "incoming"), "decision": ("accept", "withdraw")},
}
MINUTES = {"begins", "ends", "raw_from", "raw_to", "raw", "at"}
COUNTS = {"incident_rev", "handoff_rev", "slots", "free", "rate"}


def _value(kind, name, value):
    allowed = CHOICES.get(kind, {}).get(name)
    if allowed is not None and value not in allowed:
        raise DomainError(f"{kind}.{name} must be one of {', '.join(allowed)}")
    if name in MINUTES:
        return require_minute(value, name)
    if name in COUNTS:
        if type(value) is not int or not 0 <= value <= 1_000_000_000:
            raise DomainError(f"{name} must be an integer in 0..1000000000")
    elif name == "offset":
        if type(value) is not int or abs(value) > 720:
            raise DomainError("offset must be an integer in -720..720")
    elif name == "active":
        if type(value) is not bool:
            raise DomainError("active must be a JSON boolean")
    else:
        require_text(value, name)
    return value


def _append(db, event):
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str) or event["kind"] not in LAYOUT:
        raise DomainError("unknown evidence kind")
    kind = event["kind"]
    field_names, immutable = LAYOUT[kind]
    fields = field_names.split()
    if set(event) != {"kind", *fields}:
        raise DomainError(f"{kind} fields must be: {field_names}")
    values = [_value(kind, field, event[field]) for field in fields]
    if immutable:
        old = db.execute(f"SELECT * FROM {kind} WHERE id = ? ORDER BY rev DESC LIMIT 1",
                         (event["id"],)).fetchone()
        if old is not None and any(old[key] != event[key] for key in immutable.split()):
            raise DomainError(f"{kind} {event['id']} cannot move to a different scope")
    if kind == "visit" and db.execute("""SELECT 1 FROM visit WHERE car = ?
        AND begins < ? AND ends > ?""", (event["car"], event["ends"], event["begins"])).fetchone():
        raise DomainError("the same car already has an overlapping visit window")
    rev = db.execute("INSERT INTO revision(action, payload) VALUES (?, ?)", (kind, packed(event))).lastrowid
    columns = ", ".join([*fields, "rev"])
    placeholders = ", ".join("?" for _ in range(len(fields) + 1))
    db.execute(f"INSERT INTO {kind} ({columns}) VALUES ({placeholders})", (*values, rev))
    return rev


def capture(db, document):
    if not isinstance(document, dict) or set(document) != {"batch", "events"}:
        raise DomainError("capture requires exactly batch and events")
    batch_id = require_text(document["batch"], "batch")
    events = document["events"]
    if not isinstance(events, list) or not events:
        raise DomainError("events must be a nonempty array")
    payload = packed(events)
    with writing(db):
        previous = db.execute("SELECT * FROM batch WHERE id = ?", (batch_id,)).fetchone()
        if previous:
            if previous["payload"] != payload:
                raise DomainError(f"batch {batch_id} already has a different payload")
            return {"batch": batch_id, "first_rev": previous["first_rev"],
                    "last_rev": previous["last_rev"], "replayed": True}
        first = current(db) + 1
        for event in events:
            last = _append(db, event)
        db.execute("INSERT INTO batch VALUES (?, ?, ?, ?)", (batch_id, payload, first, last))
        return {"batch": batch_id, "first_rev": first, "last_rev": last, "replayed": False}
