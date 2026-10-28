"""Read-only, private deployment invariants for the book-club database.

Only hashes of values leave SQLite. Runtime caches and navigation publications
are deliberately excluded: refreshing those is part of an ordinary deployment.
This module is standalone so it can run inside either the old or the new image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


FORMAT = 1
# New columns are captured automatically and compared only when present before
# deployment. Do not include caches whose legitimate refresh would mask a real
# failure by making every deployment report a mismatch.
TABLES = {
    "bc_books": {"revision", "deadline"},
    "bc_essays": set(),
    "bc_meetings": {"revision", "start", "end", "voice_id", "status", "host_id",
                    "host_state", "offer_until", "host_version", "plan_generation", "exhausted",
                    "event_status_confirmed", "last_confirmed_status"},
    "bc_plans": set(),
    "bc_club_format": {"revision", "updated_at"},
    "bc_import_budgets": set(),
    "bc_import_runs": {"updated_at"},
    "bc_import_sources": set(),
    "bc_import_plan_restores": set(),
    "bc_import_restyle_audit": set(),
    "bc_import_restyle_threads": set(),
    "bc_import_chunk_plans": set(),
    "bc_import_preparation": set(),
    "bc_import_selections": set(),
    "bc_book_trash_audit": set(),
    "bc_book_removal_operations": {"updated_at"},
    "bc_book_removal_resources": {"updated_at"},
    "bc_book_removal_retries": set(),
    "bc_book_removal_tombstones": set(),
}
PUBLICATION_COLUMNS = ("key", "guild_id", "channel_id", "message_id", "webhook_id")


class SnapshotError(RuntimeError):
    """A diagnostic containing no database values, SQL payloads, or tokens."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _typed(value):
    if value is None:
        return ["null"]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    return ["text", value]


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _columns(db, table):
    return db.execute("PRAGMA table_info(" + _quote(table) + ")").fetchall()


def _capture_table(db, table, excluded):
    info = _columns(db, table)
    columns = [column["name"] for column in info if column["name"] not in excluded]
    primary = [column["name"] for column in sorted(info, key=lambda item: item["pk"]) if column["pk"]]
    if not primary:
        raise SnapshotError("A protected table has no primary key: " + table)
    rows = []
    for row in db.execute("SELECT * FROM " + _quote(table)):
        identity = _json([_typed(row[column]) for column in primary])
        rows.append((identity, row))
    rows.sort(key=lambda item: item[0])
    hashes = {}
    for column in columns:
        digest = hashlib.sha256()
        for identity, row in rows:
            # Length prefixes avoid ambiguous boundaries and bind every value
            # to its original row even when two rows exchange their contents.
            value = _json(_typed(row[column]))
            for part in (identity, value):
                digest.update(len(part).to_bytes(8, "big"))
                digest.update(part)
        hashes[column] = digest.hexdigest()
    return {"mode": "exact", "count": len(rows), "columns": hashes}


def _protected_publication(key):
    return ((key.startswith("book:") and key.count(":") == 1)
            or key.startswith(("essay-space:", "essay-import:")))


def _capture_publications(db):
    available = {column["name"] for column in _columns(db, "bc_publications")}
    required = {"key", "state", "channel_id", "message_id"}
    if not required <= available:
        raise SnapshotError("Protected publication identity columns are missing.")
    columns = [column for column in PUBLICATION_COLUMNS if column in available]
    rows = {}
    for row in db.execute("SELECT * FROM bc_publications WHERE state='ready' "
                          "AND channel_id IS NOT NULL AND message_id IS NOT NULL"):
        if _protected_publication(row["key"]):
            identity = _hash(_typed(row["key"]))
            rows[identity] = {column: _hash(_typed(row[column])) for column in columns}
    return {"mode": "subset", "count": len(rows), "columns": columns, "rows": rows}


def capture(dbpath):
    """Capture committed state without creating a database or running migrations.

    A single explicit read transaction also works with an active WAL writer.
    Missing files and databases without book-club tables are explicit states.
    """
    result = {"format": FORMAT, "database_exists": False, "schema": None,
              "active_imports": 0, "tables": {}}
    path = Path(dbpath)
    try:
        if not path.exists():
            return result
        if not path.is_file():
            raise SnapshotError("Database path is not a regular file.")
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
                raise SnapshotError("Database structural check failed.")
            present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schema = 0
            if "bc_migrations" in present:
                schema = db.execute("SELECT COALESCE(MAX(version),0) FROM bc_migrations").fetchone()[0]
                if not isinstance(schema, int) or schema < 0:
                    raise SnapshotError("Book-club schema version is invalid.")
            result.update(database_exists=True, schema=schema)
            if "bc_import_runs" in present:
                if "state" not in {column["name"] for column in _columns(db, "bc_import_runs")}:
                    raise SnapshotError("Import state column is missing.")
                result["active_imports"] = db.execute(
                    "SELECT COUNT(*) FROM bc_import_runs WHERE state IN ('running','applying')"
                ).fetchone()[0]
            for table, excluded in TABLES.items():
                if table in present:
                    result["tables"][table] = _capture_table(db, table, excluded)
            if "bc_publications" in present:
                result["tables"]["bc_publications"] = _capture_publications(db)
            db.rollback()
        finally:
            db.close()
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        raise SnapshotError("Cannot read a consistent database snapshot.") from exc
    return result


def compare(before, after):
    """Return safe diagnostics; added migration columns/tables are permitted.

    Ready book/essay publication mappings are compared as a subset: a refresh
    may create a missing mapping, but may not replace an existing Discord ID.
    Concurrent organizer edits are conservatively reported as changes too.
    """
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or before.get("format") != FORMAT or after.get("format") != FORMAT):
        raise SnapshotError("Unsupported snapshot format.")
    issues = []
    if before.get("database_exists") and not after.get("database_exists"):
        return ["Database disappeared after deployment."]
    old_schema, new_schema = before.get("schema"), after.get("schema")
    if old_schema is not None and (new_schema is None or new_schema < old_schema):
        issues.append("Book-club schema version decreased.")
    old_tables, new_tables = before.get("tables", {}), after.get("tables", {})
    for table, old in old_tables.items():
        new = new_tables.get(table)
        if new is None:
            issues.append("Protected table disappeared: " + table)
            continue
        if old.get("mode") != new.get("mode"):
            issues.append("Protected snapshot mode changed: " + table)
            continue
        if old["mode"] == "subset":
            missing = changed = 0
            for identity, cells in old["rows"].items():
                new_cells = new.get("rows", {}).get(identity)
                if new_cells is None:
                    missing += 1
                elif any(new_cells.get(column) != value for column, value in cells.items()):
                    changed += 1
            if missing or changed:
                issues.append(f"Existing book/essay publication mappings changed: {missing} missing, {changed} modified.")
            continue
        if old["count"] != new["count"]:
            issues.append("Protected row count changed: " + table)
        for column, digest in old["columns"].items():
            if column not in new.get("columns", {}):
                issues.append("Protected column disappeared: " + table + "." + column)
            elif new["columns"][column] != digest:
                issues.append("Protected data changed: " + table + "." + column)
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    capture_parser = subcommands.add_parser("capture")
    capture_parser.add_argument("database")
    compare_parser = subcommands.add_parser("compare")
    compare_parser.add_argument("before")
    compare_parser.add_argument("after")
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            output, status = capture(args.database), 0
        else:
            before = json.loads(Path(args.before).read_text(encoding="utf-8"))
            after = json.loads(Path(args.after).read_text(encoding="utf-8"))
            output = compare(before, after)
            status = int(bool(output))
        print(json.dumps(output, sort_keys=True, ensure_ascii=True))
        return status
    except (SnapshotError, OSError, ValueError, TypeError, KeyError):
        print("State snapshot could not be verified; private database values were omitted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
