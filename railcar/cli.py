import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from . import accrual, settlement
from .capture import capture
from .db import DomainError, connect, initialize, reading, require_minute, row, snapshot, visit_at
from .liability import liability
from .operations import assess_operations
from .pricing import tariff_at


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _strict_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise DomainError(f"duplicate JSON key {key}")
        obj[key] = value
    return obj


def load(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=_strict_object)


def _as_of(parser):
    parser.add_argument("visit")
    parser.add_argument("--as-of", type=int, required=True)
    parser.add_argument("--cutoff", type=int)


def _window(parser):
    parser.add_argument("visit")
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--cutoff", type=int)


def arguments(argv):
    parser = argparse.ArgumentParser(description="Offline railcar demurrage ledger")
    parser.add_argument("--db", required=True, help="SQLite ledger path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the SQLite schema")
    for command in ("seed", "demo"):
        commands.add_parser(command)
    for command in ("capture", "invoice"):
        commands.add_parser(command).add_argument("file")
    for command in ("custody", "accrue", "tariff"):
        _as_of(commands.add_parser(command))
    for command in ("operations", "liability"):
        _window(commands.add_parser(command))
    commands.add_parser("evidence").add_argument("revision", type=int)
    commands.add_parser("audit").add_argument("run", type=int)
    commands.add_parser("balance").add_argument("invoice")
    commands.add_parser("adjustment").add_argument("id", type=int)
    proposal = commands.add_parser("prepare")
    proposal.add_argument("invoice")
    proposal.add_argument("run", type=int)
    proposal.add_argument("operator")
    approval = commands.add_parser("approve")
    approval.add_argument("adjustment", type=int)
    approval.add_argument("operator")
    return parser.parse_args(argv)


def seed(db):
    return capture(db, load(FIXTURES / "station.json"))


def demo(db):
    base = seed(db)
    original = accrual.record_run(db, "V-17", 210, base["last_rev"])
    settlement.file_invoice(db, load(FIXTURES / "invoice.json"))
    capture(db, load(FIXTURES / "late-capacity.json"))
    revised = accrual.record_run(db, "V-17", 210)
    proposal = settlement.prepare(db, "INV-17", revised["run"], "billing-clerk")
    if proposal["status"] == "proposed":
        settlement.approve(db, proposal["adjustment"], "reviewer")
    return {"original_run": original["run"], "original_totals": original["result"]["totals"],
            "revised_run": revised["run"], "revised_totals": revised["result"]["totals"],
            "original_replays": accrual.audit_run(db, original["run"])["matches"],
            "balance": settlement.balance(db, "INV-17")}


def _inspect(db, args):
    cutoff = snapshot(db, args.cutoff)
    visit = visit_at(db, args.visit, cutoff)
    if args.command in ("operations", "liability"):
        start = require_minute(args.start, "start")
        end = require_minute(args.end, "end")
        if start >= end:
            raise DomainError("start must be before end")
        inspect = assess_operations if args.command == "operations" else liability
        return {"cutoff": cutoff, "result": inspect(db, visit, cutoff, start, end)}
    at = require_minute(args.as_of, "as_of")
    if at < visit["begins"]:
        raise DomainError("as_of precedes the visit window")
    inspect = accrual.decisions if args.command == "custody" else tariff_at
    return {"cutoff": cutoff, "result": inspect(db, visit, cutoff, at)}


def dispatch(db, args):
    command = args.command
    if command == "init":
        initialize(db)
        return {"schema_version": 1}
    if command == "seed":
        return seed(db)
    if command == "demo":
        return demo(db)
    if command == "capture":
        return capture(db, load(args.file))
    if command == "invoice":
        return settlement.file_invoice(db, load(args.file))
    if command == "accrue":
        return accrual.record_run(db, args.visit, args.as_of, args.cutoff)
    if command == "audit":
        return accrual.audit_run(db, args.run)
    if command == "prepare":
        return settlement.prepare(db, args.invoice, args.run, args.operator)
    if command == "approve":
        return settlement.approve(db, args.adjustment, args.operator)
    with reading(db):
        if command == "balance":
            return settlement.balance(db, args.invoice)
        if command == "adjustment":
            return settlement.inspect_adjustment(db, args.id)
        if command == "evidence":
            item = row(db, "SELECT rev, payload FROM revision WHERE rev = ?", (args.revision,), "revision not found")
            return {"revision": item["rev"], "event": json.loads(item["payload"])}
        return _inspect(db, args)


def main(argv=None):
    args = arguments(argv)
    try:
        if args.command not in ("init", "demo") and (args.db == ":memory:" or not Path(args.db).is_file()):
            raise DomainError("database does not exist: initialize it first")
        with closing(connect(args.db)) as db:
            if args.command == "demo":
                initialize(db)
            elif args.command != "init" and db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise DomainError("database does not have a supported initialized schema")
            result = dispatch(db, args)
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
        return int(args.command == "audit" and not result["matches"])
    except (DomainError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
