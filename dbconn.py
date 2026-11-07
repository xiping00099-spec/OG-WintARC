"""
dbconn.py - Turso (libSQL) compatibility layer.

The rest of the project talks to the database through a `sqlite3`-style connection object (execute(), fetchone(),
row["column"], in_transaction, lastrowid, rowcount). Turso's Python client is almost compatible but lacks `row_factory`,
`backup()`, and raises different errors. `RemoteConnection` closes those gaps so NO query, schema or business rule changes:

* rows behave like sqlite3.Row (index, name, keys(), slicing)
* one shared connection protected by an RLock (`tx_lock`): database.transaction() holds it for the WHOLE transaction, every
  other statement takes it per call, so the bot, the website threads and the scheduler can never interleave statements
  into somebody else's transaction
* connection failures: reads (and BEGIN) are retried with a fresh connection; writes are NEVER blindly retried (a retry after
  an ambiguous failure could duplicate data); an interrupted transaction raises DatabaseUnavailable and the server discards it
* constraint errors are mapped to sqlite3.IntegrityError, SQL mistakes to sqlite3.OperationalError
* a read-only view for the website, a SQLite snapshot (`dump_to_sqlite`) for backups and `restore_from_sqlite`

The `libsql` package is imported lazily, so the default SQLite mode never needs it.
"""
import contextlib
import gzip
import logging
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class DatabaseUnavailable(Exception):
    """The database could not be reached or the operation was interrupted. Nothing is silently lost or faked."""

    def __init__(self, message, *, during_transaction=False, ambiguous=False):
        super().__init__(message)
        self.during_transaction = during_transaction  # a transaction was open: the server discards it
        self.ambiguous = ambiguous                    # COMMIT was sent but the answer never arrived: it may have been saved


class DatabaseConfigError(DatabaseUnavailable):
    """Wrong URL / token. Retrying cannot help."""


# ------------------------------------------------------------------ rows
class Row:
    """Minimal drop-in for sqlite3.Row: row[0], row["name"] (case-insensitive), keys(), iteration, slicing, len()."""
    __slots__ = ("_names", "_index", "_values")

    def __init__(self, names, index, values):
        self._names, self._index, self._values = names, index, tuple(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._index[key.lower()]]
            except KeyError:
                raise IndexError("No item with that key") from None
        return self._values[key]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        return self._values == (other._values if isinstance(other, Row) else other)

    def __hash__(self):
        return hash(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))})"


class Cursor:
    """Result of execute(): all rows are already fetched (inside the lock and the retry logic)."""

    def __init__(self, rows, description, lastrowid, rowcount):
        self._rows, self._pos = rows, 0
        self.description, self.lastrowid, self.rowcount = description, lastrowid, rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self):
        rest, self._pos = self._rows[self._pos:], len(self._rows)
        return rest

    def fetchmany(self, size=1):
        chunk = self._rows[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __iter__(self):
        return iter(self.fetchall())


# ------------------------------------------------------------------ statement / error classification
_FIRST_WORD = re.compile(r"^\s*(?:--[^\n]*\n\s*)*([a-zA-Z]+)")
_INTEGRITY = ("unique constraint failed", "foreign key constraint failed", "check constraint failed", "not null constraint failed",
              "constraint failed", "primary key must be unique")
_AUTH = ("401", "403", "unauthorized", "unauthenticated", "authentication failed", "auth token", "jwt", "invalid token", "forbidden")
_SQL = ("syntax error", "no such table", "no such column", "no such index", "incomplete input", "no transaction is active",
        "cannot start a transaction", "cannot commit", "datatype mismatch", "too many sql variables", "has no column",
        "unrecognized token", "already exists", "readonly database", "misuse", "wrong number of", "bindings supplied",
        "no such savepoint", "no such function", "ambiguous column")


def _word(sql) -> str:
    m = _FIRST_WORD.match(sql)
    return m.group(1).lower() if m else ""


def statement_kind(sql) -> str:
    """'tx' (BEGIN/COMMIT/ROLLBACK/SAVEPOINT/RELEASE), 'read' or 'write'."""
    w = _word(sql)
    if w in ("begin", "commit", "end", "rollback", "savepoint", "release"):
        return "tx"
    if w in ("select", "with", "explain"):
        return "read"
    if w == "pragma":
        return "write" if "=" in sql else "read"
    return "write"


def classify(exc) -> str:
    low = str(exc).lower()
    if any(k in low for k in _INTEGRITY):
        return "integrity"
    if any(k in low for k in _AUTH):
        return "auth"
    if any(k in low for k in _SQL):
        return "sql"
    return "connection"  # anything unknown is treated as a connection problem: reconnect, never guess


def is_read_only_sql(sql) -> bool:
    """True only for plain reads. Used by the website's read-only view."""
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        return False
    w = _word(body)
    if w in ("select", "explain"):
        return True
    if w == "with":
        return not re.search(r"\b(insert|update|delete|replace|drop|alter|create|attach|detach|vacuum|pragma)\b", body, re.I)
    if w == "pragma":
        return "=" not in body
    return False


# ------------------------------------------------------------------ the shared connection
class RemoteConnection:
    is_remote = True

    def __init__(self, open_fn, *, shared=False, secrets=(), sleep=time.sleep, retries=(0.2, 0.6, 1.5), label="turso"):
        self._open_fn = open_fn
        self._raw = None
        self._in_tx = False
        self.tx_lock = threading.RLock()
        self.shared = shared
        self.label = label
        self._secrets = [s for s in secrets if s]
        self._sleep, self._retries = sleep, tuple(retries)
        self.reconnects = 0

    # -- helpers
    def _clean(self, text) -> str:
        text = str(text)
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    def _ensure(self):
        if self._raw is None:
            self._raw = self._open_fn()
            self.reconnects += 1
            for pragma in ("PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=5000"):  # per-connection settings, re-applied on every reconnect
                try:
                    self._raw.execute(pragma)
                except Exception as e:  # noqa: BLE001 - a server that rejects a pragma must not stop the app
                    log.debug("pragma %s not applied: %s", pragma, self._clean(e))
        return self._raw

    def _drop(self):
        raw, self._raw = self._raw, None
        if raw is not None:
            with contextlib.suppress(Exception):
                raw.close()

    def _fail(self, exc, kind, during_tx):
        """Convert a driver error into our exceptions; reconnects lazily after connection problems."""
        msg = self._clean(exc)
        cls = classify(exc)
        if cls == "integrity":
            raise sqlite3.IntegrityError(msg) from None
        if cls == "sql":
            raise sqlite3.OperationalError(msg) from None
        if cls == "auth":
            self._drop()
            raise DatabaseConfigError("The database rejected the credentials (check TURSO_AUTH_TOKEN).") from None
        self._drop()
        was_tx, self._in_tx = self._in_tx, False
        log.warning("Database connection problem (%s): %s", self.label, msg[:200])
        raise DatabaseUnavailable("The database is not reachable right now.", during_transaction=was_tx or during_tx)

    # -- public API (sqlite3-compatible subset)
    def execute(self, sql, params=()):
        kind = statement_kind(sql)
        with self.tx_lock:
            word = _word(sql)
            if kind == "tx":
                return self._execute_tx(sql, word)
            retry = kind == "read" and not self._in_tx
            attempts = 1 + (len(self._retries) if retry else 0)
            for attempt in range(attempts):
                try:
                    raw = self._ensure()
                    cur = raw.execute(sql, tuple(params))
                    return self._wrap(raw, cur, sql, word)
                except DatabaseUnavailable:
                    raise
                except (sqlite3.IntegrityError, sqlite3.OperationalError):
                    raise
                except Exception as e:  # noqa: BLE001
                    try:
                        self._fail(e, kind, False)
                    except DatabaseConfigError:
                        raise                      # wrong credentials: retrying cannot help
                    except DatabaseUnavailable:
                        if attempt + 1 >= attempts:
                            raise
                        self._sleep(self._retries[attempt])

    def _wrap(self, raw, cur, sql, word):
        names = [d[0] for d in (cur.description or [])]
        rows = []
        if names:
            index = {n.lower(): i for i, n in enumerate(names)}
            rows = [Row(names, index, r) for r in cur.fetchall()]
        lastrowid = getattr(cur, "lastrowid", None)
        rowcount = getattr(cur, "rowcount", None)
        if word in ("insert", "replace") and not lastrowid:
            lastrowid = raw.execute("SELECT last_insert_rowid()").fetchone()[0]
        if word in ("insert", "replace", "update", "delete") and (rowcount is None or rowcount < 0):
            rowcount = raw.execute("SELECT changes()").fetchone()[0]
        return Cursor(rows, cur.description, lastrowid, rowcount)

    def _execute_tx(self, sql, word):
        text = sql.strip().lower()
        dead = self._raw is None
        if dead and self._in_tx and word in ("commit", "end"):
            # A transaction belongs to the connection that opened it.  Never
            # reconnect and send COMMIT on a fresh connection: that produces
            # "cannot commit - no transaction is active" and hides the real
            # event (the original transaction was interrupted when its
            # connection disappeared).  The server discards that transaction
            # when the old connection closes, so this is a known-not-committed
            # outcome, not an ambiguous commit.
            was_tx, self._in_tx = self._in_tx, False
            raise DatabaseUnavailable(
                "The database connection was lost before the transaction could be committed.",
                during_transaction=was_tx,
                ambiguous=False,
            )
        if word in ("rollback", "release", "end") and dead:
            self._in_tx = False if text == "rollback" else self._in_tx
            return Cursor([], None, None, 0)  # nothing to undo: the old connection (and its transaction) is gone
        attempts = 1 + (len(self._retries) if word == "begin" and not self._in_tx else 0)
        for attempt in range(attempts):
            try:
                raw = self._ensure()
                cur = raw.execute(sql)
                if word == "begin":
                    self._in_tx = True
                elif word in ("commit", "end") or text == "rollback":
                    self._in_tx = False
                return Cursor([], None, None, 0)
            except (DatabaseUnavailable,):
                raise
            except Exception as e:  # noqa: BLE001
                cls = classify(e)
                if word == "rollback" and cls in ("sql", "connection"):
                    self._in_tx = False       # "no transaction is active" / dead connection: already rolled back
                    if cls == "connection":
                        self._drop()
                    return Cursor([], None, None, 0)
                if cls == "sql" and word in ("rollback", "release"):
                    return Cursor([], None, None, 0)
                try:
                    self._fail(e, "tx", True)
                except DatabaseUnavailable as du:
                    if word == "commit":
                        du.ambiguous = True
                        du.during_transaction = True
                        raise
                    if attempt + 1 >= attempts:
                        raise
                    self._sleep(self._retries[attempt])

    def executemany(self, sql, seq):
        last = None
        with self.tx_lock:
            for params in seq:
                last = self.execute(sql, params)
        return last

    def ping(self):
        return self.execute("SELECT 1").fetchone()[0] == 1

    def wait_until_ready(self, waits=(1, 2, 4, 8, 10, 10)):
        """Used at startup: keep trying for ~35 s, then give up loudly (NEVER fall back to a local file)."""
        last = None
        for i in range(len(waits) + 1):
            try:
                self.ping()
                return True
            except DatabaseConfigError:
                raise
            except DatabaseUnavailable as e:
                last = e
                if i < len(waits):
                    log.warning("Database not ready (attempt %s/%s); retrying in %ss", i + 1, len(waits) + 1, waits[i])
                    self._sleep(waits[i])
        raise last

    def close(self):
        if not self.shared:
            self.shutdown()

    def shutdown(self):
        with self.tx_lock:
            self._drop()
            self._in_tx = False


class ReadOnlyView:
    """What the website receives: the same shared connection, but anything that could change data is refused."""
    is_remote = True

    def __init__(self, conn):
        self._conn = conn
        self.tx_lock = conn.tx_lock

    def execute(self, sql, params=()):
        if not is_read_only_sql(sql):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return self._conn.execute(sql, params)

    @property
    def in_transaction(self):
        return False

    def close(self):
        self._conn.close()


_shared = None
_shared_lock = threading.Lock()


def _open_libsql(url, token):
    import libsql  # lazy: only needed when Turso is configured
    return libsql.connect(database=url, auth_token=token, isolation_level=None)  # autocommit; we send BEGIN/COMMIT ourselves


def shared_remote(url, token) -> RemoteConnection:
    """The ONE connection used by the bot, the website and the scheduler."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = RemoteConnection(lambda: _open_libsql(url, token), shared=True, secrets=[token])
        return _shared


def reset_shared():
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.shutdown()
        _shared = None


# ------------------------------------------------------------------ snapshots (backups) and restore
def _lock_of(conn):
    return getattr(conn, "tx_lock", None) or contextlib.nullcontext()


def _quote(name) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _schema_version(conn) -> int:
    v = 0
    with contextlib.suppress(Exception):
        v = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if getattr(conn, "is_remote", False):
        with contextlib.suppress(Exception):
            row = conn.execute("SELECT value FROM bot_settings WHERE key='schema_version'").fetchone()
            if row:
                v = max(v, int(row[0]))
    return v


def table_counts(conn) -> dict:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    return {n: conn.execute(f"SELECT COUNT(*) FROM {_quote(n)}").fetchone()[0] for n in names}


def dump_to_sqlite(conn, dest_path) -> dict:
    """Write a complete, consistent copy of the database into a new plain SQLite file. Returns the row counts."""
    if Path(dest_path).exists():
        raise FileExistsError(f"refusing to overwrite an existing file: {dest_path}")
    dest_path = str(dest_path)
    with _lock_of(conn):  # nobody in this process writes while we copy
        tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall()
        indexes = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY rowid").fetchall()
        has_seq = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() is not None
        began = False
        with contextlib.suppress(Exception):
            if not getattr(conn, "in_transaction", False):
                conn.execute("BEGIN")  # one read snapshot for all tables
                began = True
        dest = sqlite3.connect(dest_path)
        try:
            dest.execute("PRAGMA foreign_keys=OFF")
            for t in tables:
                dest.execute(t["sql"])
            counts = {}
            for t in tables:
                rows = conn.execute(f"SELECT * FROM {_quote(t['name'])}").fetchall()
                if rows:
                    dest.executemany(f"INSERT INTO {_quote(t['name'])} VALUES ({','.join('?' * len(rows[0]))})", [tuple(r) for r in rows])
                counts[t["name"]] = len(rows)
            if has_seq:
                dest.execute("DELETE FROM sqlite_sequence")
                dest.executemany("INSERT INTO sqlite_sequence(name, seq) VALUES(?,?)", [tuple(r) for r in conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall()])
            for ix in indexes:
                dest.execute(ix["sql"])
            version = _schema_version(conn)
            dest.execute(f"PRAGMA user_version={int(version)}")
            dest.commit()
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("snapshot failed its integrity check")
            return counts
        finally:
            dest.close()
            if began:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")


def unique_path(path) -> Path:
    """A path that does not exist yet (adds -2, -3 ... before the extension). Backups and snapshots must never overwrite each other."""
    path = Path(path)
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def gzip_file(src, dst) -> Path:
    with open(src, "rb") as f_in, gzip.open(dst, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return Path(dst)


class RestoreRefused(Exception):
    """The restore was NOT started (nothing was changed)."""


REQUIRED_TABLES = {"sessions", "participants", "session_members", "daily_reports", "penalties", "bot_settings", "audit_log"}
DATA_TABLES = ("participants", "daily_reports", "daily_targets", "penalties")


def _open_source(path, workdir):
    path = Path(path)
    if not path.exists():
        raise RestoreRefused(f"Backup file not found: {path}")
    if path.suffix == ".gz":
        out = Path(workdir) / "restored-source.db"
        try:
            with gzip.open(path, "rb") as f_in, open(out, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        except OSError as e:
            raise RestoreRefused(f"The backup file is not a valid .gz file ({e}).") from None
        path = out
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        src.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError as e:
        raise RestoreRefused(f"The file is not a readable SQLite backup ({e}).") from None
    return src


def restore_from_sqlite(conn, backup_path, *, force=False, snapshot_dir=None, latest_version=1) -> dict:
    """
    Load a SQLite backup (.db or .db.gz) into the target database, all-or-nothing (one transaction).
    * refuses a target that already holds participants/reports/targets/penalties unless force=True
    * with force, a snapshot of the current target is written to snapshot_dir FIRST
    * refuses damaged or unknown-version backups before touching anything
    """
    with tempfile.TemporaryDirectory() as work:
        src = _open_source(backup_path, work)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RestoreRefused("The backup failed its integrity check; it is damaged.")
            if src.execute("PRAGMA foreign_key_check").fetchall():
                raise RestoreRefused("The backup contains broken references.")
            tables = src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall()
            names = [t["name"] for t in tables]
            missing = REQUIRED_TABLES - set(names)
            if missing:
                raise RestoreRefused(f"This is not a WINTER ARC backup (missing tables: {', '.join(sorted(missing))}).")
            version = int(src.execute("PRAGMA user_version").fetchone()[0])
            if version > latest_version:
                raise RestoreRefused(f"The backup is from a newer version (schema v{version}); update the bot first.")
            if version < 1:
                raise RestoreRefused("The backup has no schema version; refusing to guess.")
            indexes = [r["sql"] for r in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY rowid")]
            data = {n: src.execute(f"SELECT * FROM {_quote(n)}").fetchall() for n in names}
            seq = src.execute("SELECT name, seq FROM sqlite_sequence").fetchall() if src.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else []

            with _lock_of(conn):
                existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
                has_data = any(t in existing and conn.execute(f"SELECT COUNT(*) FROM {_quote(t)}").fetchone()[0] > 0 for t in DATA_TABLES)
                if has_data and not force:
                    raise RestoreRefused("The target database already contains participants/reports. Nothing was changed. "
                                         "Use a new empty database, or add --force (a snapshot of the current data is saved first).")
                snapshot = None
                if has_data:
                    if not snapshot_dir:
                        raise RestoreRefused("--force needs a snapshot directory to save the current data first.")
                    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
                    snapshot = unique_path(Path(snapshot_dir) / f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.db")
                    dump_to_sqlite(conn, snapshot)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("PRAGMA defer_foreign_keys=ON")
                    for t in tables:
                        if t["name"] not in existing:
                            conn.execute(t["sql"])
                    for n in reversed(names):
                        conn.execute(f"DELETE FROM {_quote(n)}")
                    for n in names:
                        rows = data[n]
                        if rows:
                            conn.executemany(f"INSERT INTO {_quote(n)} VALUES ({','.join('?' * len(rows[0]))})", [tuple(r) for r in rows])
                    if seq or "sqlite_sequence" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}:
                        conn.execute("DELETE FROM sqlite_sequence")
                        for name, value in seq:
                            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?,?)", (name, value))
                    for ix in indexes:
                        with contextlib.suppress(sqlite3.OperationalError):
                            conn.execute(ix)   # "already exists" when the target had the schema
                    with contextlib.suppress(sqlite3.OperationalError, DatabaseUnavailable):
                        conn.execute(f"PRAGMA user_version={int(version)}")
                    if getattr(conn, "is_remote", False):
                        conn.execute("INSERT INTO bot_settings(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
                    conn.execute("COMMIT")
                except BaseException:
                    with contextlib.suppress(Exception):
                        conn.execute("ROLLBACK")
                    raise
                after = {n: conn.execute(f"SELECT COUNT(*) FROM {_quote(n)}").fetchone()[0] for n in names}
                expected = {n: len(data[n]) for n in names}
                if getattr(conn, "is_remote", False):
                    expected["bot_settings"] = expected.get("bot_settings", 0) + (0 if any(r[0] == "schema_version" for r in data.get("bot_settings", [])) else 1)
                if after != expected:
                    raise RuntimeError(f"Restore verification failed: expected {expected}, found {after}")
                return {"tables": after, "snapshot": str(snapshot) if snapshot else None, "schema_version": version}
        finally:
            src.close()
