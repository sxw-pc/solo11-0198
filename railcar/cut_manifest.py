"""Enumerate a cut from its physically identified free leading end."""
from .coupler_topology import links_at
from .db import require_minute, require_text, row


def manifest(db, cut, cutoff, minute, front):
    require_text(cut, "cut")
    require_minute(minute, "as-of")
    row(db, "SELECT 1 FROM car WHERE id=? AND rev<=?", (front[0], cutoff), "leading car unavailable")
    links, conflicts = links_at(db, cut, cutoff, minute)
    if conflicts:
        return {"cut": cut, "status": "coupler_conflict", "conflicts": conflicts}
    if front in links:
        return {"cut": cut, "status": "leading_end_latched", "evidence": links[front][0][1]}
    cars, seen, refs = [], set(), []
    cursor = front
    while cursor[0] not in seen:
        car, end = cursor
        seen.add(car)
        cars.append({"car": car, "leading_end": end})
        tail = (car, "B" if end == "A" else "A")
        if tail not in links:
            return {"cut": cut, "status": "enumerated", "cars": cars, "rear": tail, "links": refs}
        cursor, joint = links[tail][0]
        refs.append(joint)
    return {"cut": cut, "status": "cycle", "links": refs}
