#!/usr/bin/env python
"""
Turso connection self-test. Run it against a SEPARATE TEST database first (never needed for production data).

    TURSO_DATABASE_URL=libsql://...  TURSO_AUTH_TOKEN=...  python tools/turso_check.py        (or put them in .env)
    python tools/turso_check.py --counts            also print the row counts of the WINTER ARC tables (to compare after a restore)

It only creates and drops its own temporary tables (_wa_selftest_*). The token is never printed.
Exit code 0 = every REQUIRED check passed. This script is the only thing that can prove the real Turso behaviour; the project's
automated tests use a local libSQL file because the build sandbox cannot reach Turso.
"""
import os
import sqlite3
import statistics
import sys
import threading
import time
import tempfile
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402,F401  (loads .env)
import dbconn  # noqa: E402


def main(argv):
    url, token = os.environ.get("TURSO_DATABASE_URL", "").strip(), os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        print("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (environment or .env).")
        return 2
    print(f"Target: {urlparse(url).hostname}   (token hidden)\n")
    new = lambda label="check": dbconn.RemoteConnection(lambda: dbconn._open_libsql(url, token), secrets=[token], label=label)  # noqa: E731
    conn, results = new(), []
    tname = "_wa_selftest_" + os.urandom(3).hex()
    child = tname + "_child"

    def check(name, fn, required=True):
        try:
            detail = fn()
            results.append((True, required, name))
            print(f"  PASS  {name}" + (f"  — {detail}" if detail else ""))
        except Exception as e:  # noqa: BLE001
            msg = str(e).replace(token, "***")[:160]
            results.append((False, required, name))
            print(f"  {'FAIL' if required else 'INFO'}  {name}  — {type(e).__name__}: {msg}")

    def expect(cond, msg="unexpected result"):
        if not cond:
            raise AssertionError(msg)

    try:
        def latency():
            samples = []
            for _ in range(5):
                t0 = time.perf_counter()
                expect(conn.ping())
                samples.append((time.perf_counter() - t0) * 1000)
            return f"median {statistics.median(samples):.0f} ms, worst {max(samples):.0f} ms per query"
        check("connect + ping + latency", latency)

        db_has_app = conn.execute("SELECT 1 FROM sqlite_master WHERE name='participants'").fetchone()
        print("  NOTE  this database already contains WINTER ARC tables; only the _wa_selftest tables are touched." if db_has_app else
              "  NOTE  empty database (good for a test).")

        def ddl():
            conn.execute(f"CREATE TABLE {tname}(id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT, n INTEGER DEFAULT 0, s TEXT DEFAULT 'active')")
            conn.execute(f"CREATE UNIQUE INDEX {tname}_ux ON {tname}(k) WHERE s='active'")
            conn.execute(f"CREATE TABLE {child}(id INTEGER PRIMARY KEY, parent INTEGER NOT NULL REFERENCES {tname}(id))")
        check("create tables + partial unique index", ddl)

        def autocommit_visible():
            cur = conn.execute(f"INSERT INTO {tname}(k) VALUES('a')")
            other = new("second-connection")
            try:
                expect(other.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0] == 1, "a second connection cannot see the committed row")
            finally:
                other.shutdown()
            return f"lastrowid={cur.lastrowid}, rowcount={cur.rowcount}"
        check("autocommit write is visible to a second connection", autocommit_visible)

        def lastrowid_rowcount():
            expect(conn.execute(f"INSERT INTO {tname}(k) VALUES('b')").lastrowid == 2, "lastrowid wrong")
            expect(conn.execute(f"UPDATE {tname} SET n=5 WHERE k='b'").rowcount == 1, "rowcount wrong")
            expect(conn.execute(f"UPDATE {tname} SET n=5 WHERE k='nope'").rowcount == 0, "rowcount for no match should be 0")
        check("lastrowid and rowcount (single-use tokens and reminders depend on them)", lastrowid_rowcount)

        def transactions():
            other = new("second-connection")
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"INSERT INTO {tname}(k) VALUES('tx1')")
                expect(other.execute(f"SELECT COUNT(*) FROM {tname} WHERE k='tx1'").fetchone()[0] == 0, "uncommitted row leaked to another connection")
                conn.execute("ROLLBACK")
                expect(conn.execute(f"SELECT COUNT(*) FROM {tname} WHERE k='tx1'").fetchone()[0] == 0, "rollback did not undo the insert")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"INSERT INTO {tname}(k) VALUES('tx2')")
                conn.execute("SAVEPOINT s1")
                conn.execute(f"INSERT INTO {tname}(k) VALUES('tx3')")
                conn.execute("ROLLBACK TO s1")
                conn.execute("RELEASE s1")
                conn.execute("COMMIT")
                expect(other.execute(f"SELECT COUNT(*) FROM {tname} WHERE k='tx2'").fetchone()[0] == 1, "committed row not visible")
                expect(other.execute(f"SELECT COUNT(*) FROM {tname} WHERE k='tx3'").fetchone()[0] == 0, "savepoint rollback failed")
            finally:
                other.shutdown()
        check("transactions: commit, rollback, savepoint", transactions)

        def integrity():
            try:
                conn.execute(f"INSERT INTO {tname}(k) VALUES('a')")
            except sqlite3.IntegrityError:
                return "duplicate is rejected (this is what prevents duplicate penalties/reports)"
            raise AssertionError("duplicate was accepted")
        check("unique index rejects duplicates (mapped to IntegrityError)", integrity)

        def fk():
            try:
                conn.execute(f"INSERT INTO {child}(parent) VALUES(999)")
            except sqlite3.IntegrityError:
                return "foreign keys are enforced"
            raise AssertionError("foreign keys are NOT enforced on this connection (the app still works; it checks references itself)")
        check("foreign-key enforcement", fk, required=False)

        def pragma_version():
            try:
                conn.execute("PRAGMA user_version=3")
                v = conn.execute("PRAGMA user_version").fetchone()[0]
                conn.execute("PRAGMA user_version=0")
                return f"supported (read back {v})" if v == 3 else f"accepted but not persisted (read back {v}); the app also stores the version in bot_settings"
            except Exception as e:  # noqa: BLE001
                return f"not supported ({type(e).__name__}); the app falls back to bot_settings"
        check("PRAGMA user_version", pragma_version, required=False)

        def concurrency():
            conn.execute(f"INSERT INTO {tname}(k, n) VALUES('counter', 0)")
            def work():
                for _ in range(5):
                    with conn.tx_lock:
                        conn.execute("BEGIN IMMEDIATE")
                        n = conn.execute(f"SELECT n FROM {tname} WHERE k='counter'").fetchone()[0]
                        conn.execute(f"UPDATE {tname} SET n=? WHERE k='counter'", (n + 1,))
                        conn.execute("COMMIT")
            threads = [threading.Thread(target=work) for _ in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            n = conn.execute(f"SELECT n FROM {tname} WHERE k='counter'").fetchone()[0]
            expect(n == 20, f"lost updates: counter is {n}, expected 20")
            return "4 threads x 5 transactions, no lost update"
        check("concurrent transactions are serialized", concurrency)

        def reconnect():
            conn._drop()   # simulate a dropped connection
            expect(conn.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0] >= 3, "read after reconnect failed")
            return f"reconnected automatically ({conn.reconnects} connections opened)"
        check("reconnect after a dropped connection", reconnect)

        def interrupted():
            other = new("second-connection")
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"INSERT INTO {tname}(k) VALUES('interrupted')")
                conn._drop()   # the connection dies in the middle of a transaction
                try:
                    conn.execute("COMMIT")
                    raise AssertionError("COMMIT on a dead connection must fail")
                except dbconn.DatabaseUnavailable as e:
                    expect(e.during_transaction, "failure not flagged as interrupted transaction")
                expect(other.execute(f"SELECT COUNT(*) FROM {tname} WHERE k='interrupted'").fetchone()[0] == 0, "an interrupted transaction left data behind")
                expect(not conn.in_transaction, "connection still thinks a transaction is open")
                conn.execute(f"SELECT 1")
                return "uncommitted data was discarded by the server"
            finally:
                other.shutdown()
        check("interrupted transaction leaves nothing behind", interrupted)

        def snapshot():
            with tempfile.TemporaryDirectory() as d:
                counts = dbconn.dump_to_sqlite(conn, Path(d) / "snap.db")
                check_db = sqlite3.connect(Path(d) / "snap.db")
                n = check_db.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
                check_db.close()
            expect(n == counts[tname] and n >= 3, "snapshot content differs")
            return f"{len(counts)} tables copied into a plain SQLite file"
        check("snapshot (backup) to a SQLite file", snapshot)

        if "--counts" in argv:
            print("\nWINTER ARC table row counts:")
            for name, n in dbconn.table_counts(conn).items():
                if not name.startswith("_wa_selftest"):
                    print(f"  {name:20s} {n}")
    finally:
        for stmt in (f"DROP TABLE IF EXISTS {child}", f"DROP TABLE IF EXISTS {tname}"):
            try:
                conn.execute(stmt)
            except Exception:  # noqa: BLE001
                pass
        conn.shutdown()
    failed = [n for ok, req, n in results if not ok and req]
    print(f"\n{'ALL REQUIRED CHECKS PASSED' if not failed else 'FAILED: ' + '; '.join(failed)}  "
          f"({sum(1 for ok, _, _ in results if ok)}/{len(results)} checks passed; INFO lines are advisory)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
