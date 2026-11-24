PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS revision (rev INTEGER PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch (id TEXT PRIMARY KEY, payload TEXT NOT NULL, first_rev INTEGER NOT NULL, last_rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS car (id TEXT PRIMARY KEY, rev INTEGER NOT NULL REFERENCES revision);
CREATE TABLE IF NOT EXISTS cut_link (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, cut TEXT NOT NULL,
    first_car TEXT NOT NULL REFERENCES car, first_end TEXT NOT NULL,
    second_car TEXT NOT NULL REFERENCES car, second_end TEXT NOT NULL,
    at INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY (id, rev), CHECK (first_car <> second_car)
);
CREATE TABLE IF NOT EXISTS visit (
    id TEXT PRIMARY KEY, car TEXT NOT NULL REFERENCES car, station TEXT NOT NULL,
    gate TEXT NOT NULL, initial_payer TEXT NOT NULL, begins INTEGER NOT NULL,
    ends INTEGER NOT NULL CHECK (ends > begins), rev INTEGER NOT NULL REFERENCES revision
);
CREATE TABLE IF NOT EXISTS mark (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, label TEXT NOT NULL,
    car TEXT NOT NULL REFERENCES car, begins INTEGER NOT NULL, ends INTEGER NOT NULL CHECK (ends > begins),
    active INTEGER NOT NULL CHECK (active IN (0, 1)), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS clock (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, device TEXT NOT NULL,
    raw_from INTEGER NOT NULL, raw_to INTEGER NOT NULL CHECK (raw_to > raw_from),
    offset INTEGER NOT NULL CHECK (offset BETWEEN -720 AND 720), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS observation (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, visit TEXT NOT NULL REFERENCES visit,
    phase TEXT NOT NULL CHECK (phase IN ('in', 'out')), side TEXT NOT NULL CHECK (side IN ('carrier', 'yard')),
    device TEXT NOT NULL, raw INTEGER NOT NULL, label TEXT NOT NULL, gate TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS inspection (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, visit TEXT NOT NULL REFERENCES visit,
    at INTEGER NOT NULL, result TEXT NOT NULL CHECK (result IN ('ready', 'rejected')), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS incident (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, visit TEXT NOT NULL REFERENCES visit,
    begins INTEGER NOT NULL, ends INTEGER NOT NULL CHECK (ends > begins),
    cause TEXT NOT NULL CHECK (cause IN ('equipment', 'carrier', 'weather')), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS attestation (
    incident TEXT NOT NULL, incident_rev INTEGER NOT NULL, rev INTEGER NOT NULL REFERENCES revision,
    side TEXT NOT NULL CHECK (side IN ('carrier', 'yard')), decision TEXT NOT NULL CHECK (decision IN ('approve', 'deny')),
    FOREIGN KEY (incident, incident_rev) REFERENCES incident (id, rev), PRIMARY KEY (incident, side, rev)
);
CREATE TABLE IF NOT EXISTS capacity (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, station TEXT NOT NULL,
    begins INTEGER NOT NULL, ends INTEGER NOT NULL CHECK (ends > begins),
    slots INTEGER NOT NULL CHECK (slots >= 0), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS handoff (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, visit TEXT NOT NULL REFERENCES visit,
    outgoing TEXT NOT NULL, incoming TEXT NOT NULL CHECK (outgoing <> incoming), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS signature (
    handoff TEXT NOT NULL, handoff_rev INTEGER NOT NULL, rev INTEGER NOT NULL REFERENCES revision,
    side TEXT NOT NULL CHECK (side IN ('outgoing', 'incoming')), at INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'withdraw')),
    FOREIGN KEY (handoff, handoff_rev) REFERENCES handoff (id, rev), PRIMARY KEY (handoff, side, rev)
);
CREATE TABLE IF NOT EXISTS tariff (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, station TEXT NOT NULL,
    begins INTEGER NOT NULL, ends INTEGER NOT NULL CHECK (ends > begins),
    free INTEGER NOT NULL CHECK (free >= 0), rate INTEGER NOT NULL CHECK (rate > 0), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY, visit TEXT NOT NULL REFERENCES visit, cutoff INTEGER NOT NULL,
    as_of INTEGER NOT NULL, result TEXT NOT NULL, UNIQUE (visit, cutoff, as_of)
);
CREATE TABLE IF NOT EXISTS invoice (id TEXT PRIMARY KEY, visit TEXT NOT NULL UNIQUE REFERENCES visit);
CREATE TABLE IF NOT EXISTS invoice_line (
    invoice TEXT NOT NULL REFERENCES invoice, payer TEXT NOT NULL, cents INTEGER NOT NULL CHECK (cents > 0),
    PRIMARY KEY (invoice, payer)
);
CREATE TABLE IF NOT EXISTS adjustment (
    id INTEGER PRIMARY KEY, invoice TEXT NOT NULL REFERENCES invoice, run INTEGER NOT NULL REFERENCES run,
    prepared_by TEXT NOT NULL, target TEXT NOT NULL, baseline TEXT NOT NULL, deltas TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval (adjustment INTEGER PRIMARY KEY REFERENCES adjustment, approved_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS visit_car ON visit (car, begins, ends);
CREATE INDEX IF NOT EXISTS observation_visit ON observation (visit, phase, id, rev);
CREATE INDEX IF NOT EXISTS incident_visit ON incident (visit, id, rev);
CREATE INDEX IF NOT EXISTS inspection_visit ON inspection (visit, id, rev);
CREATE INDEX IF NOT EXISTS handoff_visit ON handoff (visit, id, rev);
CREATE INDEX IF NOT EXISTS capacity_station ON capacity (station, id, rev);
CREATE INDEX IF NOT EXISTS tariff_station ON tariff (station, id, rev);
CREATE INDEX IF NOT EXISTS adjustment_invoice ON adjustment (invoice, id);
PRAGMA user_version = 1;
