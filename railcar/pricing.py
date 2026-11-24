"""Select the station tariff in force at delivery, then consume visit free time once."""
from .db import evidence, versioned


def tariff_at(db, visit, cutoff, delivery_at):
    tariffs = [item for item in versioned(db, "tariff", cutoff, station=visit["station"])
               if item["begins"] <= delivery_at < item["ends"]]
    result = {"evidence": [evidence(item) for item in tariffs]}
    if len(tariffs) != 1:
        return {**result, "status": "missing" if not tariffs else "conflict"}
    return {**result, "status": "applied", "free": tariffs[0]["free"], "rate": tariffs[0]["rate"]}


def meter(segments, spans, tariff):
    free = tariff["free"]
    totals = {}
    slices = []
    segment_iter, span_iter = iter(segments), iter(spans)
    segment, span = next(segment_iter, None), next(span_iter, None)
    while segment is not None and span is not None:
        left = max(segment["from"], span["from"])
        right = min(segment["to"], span["to"])
        paused = segment["paused"]
        used = 0 if paused else min(free, right - left)
        free -= used
        billed = 0 if paused else right - left - used
        payer = span["payer"]
        cents = billed * tariff["rate"]
        if cents:
            totals[payer] = totals.get(payer, 0) + cents
        slices.append({"from": left, "to": right, "payer": payer, "paused": paused,
                       "free_used": used, "billable_minutes": billed, "cents": cents})
        if segment["to"] == right:
            segment = next(segment_iter, None)
        if span["to"] == right:
            span = next(span_iter, None)
    return {"free_remaining": free, "totals": totals, "slices": slices}
