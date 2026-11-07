#!/usr/bin/env python
"""
Restore a WINTER ARC backup (.db or .db.gz) into the configured database.

    python tools/restore_backup.py path/to/winter-arc-backup-20261102-2105-IST.db.gz
    python tools/restore_backup.py backup.db.gz --target-sqlite rehearsal.db        (practice on a local file, touches nothing else)

Target: Turso if TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set, otherwise the SQLite file DB_PATH (or --target-sqlite).
Safety rules:
  * the backup is validated first (integrity, WINTER ARC tables, schema version) - a bad file changes nothing
  * a target that already holds participants/reports/targets/penalties is REFUSED unless you add --force;
    with --force a snapshot of the current data is saved to --snapshot-dir first
  * you must type RESTORE to confirm (or pass --yes)
  * everything is loaded in ONE transaction: either all of it or nothing
Stop the running app (or at least do not use the bot) while restoring.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402  (loads .env)
import database  # noqa: E402
import dbconn  # noqa: E402


def main(argv):
    ap = argparse.ArgumentParser(description="Restore a WINTER ARC backup")
    ap.add_argument("backup")
    ap.add_argument("--target-sqlite", help="restore into this local SQLite file instead of the configured database")
    ap.add_argument("--force", action="store_true", help="allow replacing existing data (a snapshot is saved first)")
    ap.add_argument("--snapshot-dir", default="backups", help="where the pre-restore snapshot goes (default ./backups)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = ap.parse_args(argv)

    url, token = os.environ.get("TURSO_DATABASE_URL", "").strip(), os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if args.target_sqlite:
        conn = sqlite3.connect(args.target_sqlite, isolation_level=None)
        conn.row_factory = sqlite3.Row
        target = f"local file {args.target_sqlite}"
    elif url and token:
        conn = dbconn.RemoteConnection(lambda: dbconn._open_libsql(url, token), secrets=[token], label="restore")
        target = f"Turso database {urlparse(url).hostname}"
    else:
        path = os.environ.get("DB_PATH", "arc.db")
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        target = f"local file {path}"
    print(f"Backup : {args.backup}\nTarget : {target}\nForce  : {'yes' if args.force else 'no (refuses to replace existing data)'}")
    if not args.yes and input('Type RESTORE to continue: ').strip() != "RESTORE":
        print("Cancelled. Nothing was changed.")
        return 1
    try:
        res = dbconn.restore_from_sqlite(conn, args.backup, force=args.force, snapshot_dir=args.snapshot_dir,
                                         latest_version=max(v for v, _ in database.MIGRATIONS))
    except dbconn.RestoreRefused as e:
        print(f"REFUSED — nothing was changed:\n  {e}")
        return 1
    print("Restored. Rows per table:")
    for name, n in res["tables"].items():
        print(f"  {name:20s} {n}")
    if res["snapshot"]:
        print(f"Snapshot of the previous data: {res['snapshot']}")
    print("Done. Start the app; it verifies the schema version on startup.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
