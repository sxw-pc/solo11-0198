"""Resolve confirmed physical links on the named ends of railcars."""
from collections import defaultdict

from .db import evidence, versioned


def links_at(db, cut, cutoff, minute):
    observations = versioned(db, "cut_link", cutoff, cut=cut)
    joints = defaultdict(list)
    for row in observations:
        if row["at"] <= minute:
            first = (row["first_car"], row["first_end"])
            second = (row["second_car"], row["second_end"])
            joints[tuple(sorted((first, second)))].append(row)
    linked = defaultdict(list)
    conflicts = []
    for joint, sightings in sorted(joints.items()):
        last = max(row["at"] for row in sightings)
        current = [row for row in sightings if row["at"] == last]
        states = {row["state"] for row in current}
        refs = [evidence(row) for row in current]
        if len(states) != 1:
            conflicts.append({"ends": list(joint), "evidence": refs})
        elif states == {"latched"}:
            first, second = joint
            linked[first].append((second, refs))
            linked[second].append((first, refs))
    conflicts.extend({"end": end, "links": alternatives} for end, alternatives in sorted(linked.items())
                     if len(alternatives) > 1)
    return linked, conflicts
