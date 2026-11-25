-- Offline weighbridge intake: zero certificates, raw weigh tickets and intake batches.
CREATE TABLE IF NOT EXISTS zero_voucher (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, device TEXT NOT NULL,
    valid_from INTEGER NOT NULL, valid_to INTEGER NOT NULL CHECK (valid_to > valid_from),
    zero_min INTEGER NOT NULL, zero_max INTEGER NOT NULL CHECK (zero_max >= zero_min),
    active INTEGER NOT NULL CHECK (active IN (0, 1)), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS weigh_ticket (
    id TEXT NOT NULL, rev INTEGER NOT NULL REFERENCES revision, visit TEXT NOT NULL REFERENCES visit,
    car TEXT NOT NULL, station TEXT NOT NULL, scale TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('gross', 'tare')), at INTEGER NOT NULL,
    reading INTEGER NOT NULL CHECK (reading >= 0),
    read_min INTEGER NOT NULL, read_max INTEGER NOT NULL CHECK (read_max >= read_min),
    window_from INTEGER NOT NULL, window_to INTEGER NOT NULL CHECK (window_to > window_from),
    active INTEGER NOT NULL CHECK (active IN (0, 1)), PRIMARY KEY (id, rev)
);
CREATE TABLE IF NOT EXISTS weigh_batch (
    id TEXT PRIMARY KEY, payload TEXT NOT NULL, first_rev INTEGER NOT NULL, last_rev INTEGER NOT NULL,
    inspection_rev INTEGER
);
CREATE INDEX IF NOT EXISTS zero_voucher_device ON zero_voucher (device, id, rev);
CREATE INDEX IF NOT EXISTS weigh_ticket_visit ON weigh_ticket (visit, id, rev);
PRAGMA user_version = 2;
