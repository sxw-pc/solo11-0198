from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


class DomainError(ValueError):
    """A rejected business command with no committed side effects."""


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2):
        raise DomainError(f"unsupported schema version {version}")
    if version == 0:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
    elif version == 1:
        migration = Path(__file__).with_name("migration_1_2.sql").read_text(encoding="utf-8")
        db.executescript("BEGIN IMMEDIATE;\n" + migration + "\nCOMMIT;")


@contextmanager
def writing(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


@contextmanager
def reading(db):
    db.execute("BEGIN")
    try:
        yield
    finally:
        db.rollback()


def current(db):
    return db.execute("SELECT COALESCE(MAX(rev), 0) FROM revision").fetchone()[0]


def snapshot(db, cutoff):
    latest = current(db)
    if cutoff is None:
        return latest
    if type(cutoff) is not int or not 0 <= cutoff <= latest:
        raise DomainError("cutoff must be an existing revision or zero")
    if cutoff and not db.execute("SELECT 1 FROM batch WHERE last_rev = ?", (cutoff,)).fetchone():
        raise DomainError("cutoff must be a committed batch boundary")
    return cutoff


def row(db, query, args, missing):
    value = db.execute(query, args).fetchone()
    if value is None:
        raise DomainError(missing)
    return value


def versioned(db, table, cutoff, key=("id",), **scope):
    """Latest row per stable key. Scope columns are immutable within a key."""
    match = " AND ".join(f"item.{column} = newer.{column}" for column in key)
    filters = "".join(f" AND item.{column} = ?" for column in scope)
    order = ", ".join(f"item.{column}" for column in key)
    return list(db.execute(f"""
        SELECT item.* FROM {table} AS item
        WHERE item.rev <= ? {filters} AND NOT EXISTS (
            SELECT 1 FROM {table} AS newer WHERE {match}
            AND newer.rev <= ? AND newer.rev > item.rev
        ) ORDER BY {order}
        """, (cutoff, *scope.values(), cutoff)))


def evidence(record, key="id"):
    return [record[key], record["rev"]]


def visit_at(db, visit_id, cutoff):
    return row(db, "SELECT * FROM visit WHERE id = ? AND rev <= ?", (visit_id, cutoff),
               f"visit {visit_id} is unavailable at revision {cutoff}")


def require_text(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DomainError(f"{name} must be a nonblank trimmed string")
    return value


def require_minute(value, name):
    if type(value) is not int or not 0 <= value <= 100_000_000:
        raise DomainError(f"{name} must be an integer minute in 0..100000000")
    return value
