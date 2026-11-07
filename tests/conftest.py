import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import database as db  # noqa: E402
import participants as ppl  # noqa: E402
import reports  # noqa: E402

ADMIN = 111
FAKE_NOW = datetime(2026, 11, 10, 20, 0, tzinfo=config.TZ)


@pytest.fixture(autouse=True)
def fixed_clock():
    """Every test runs at 2026-11-10 20:00 IST unless it changes config.now_func itself."""
    old = config.now_func
    config.now_func = lambda: FAKE_NOW
    yield
    config.now_func = old


def _libsql_factory(path):
    """A RemoteConnection over a LOCAL libSQL file: exactly the code path Turso uses, minus the network."""
    import libsql
    import dbconn

    def open_raw():
        raw = libsql.connect(str(path), isolation_level=None)
        raw.execute("PRAGMA journal_mode=WAL")
        return raw
    return dbconn.RemoteConnection(open_raw, label="libsql-test", sleep=lambda s: None)


@pytest.fixture(autouse=True)
def _backend():
    """WA_TEST_BACKEND=libsql runs the same tests through the Turso compatibility layer instead of plain SQLite."""
    import os
    if os.environ.get("WA_TEST_BACKEND") == "libsql":
        db.configure_backend_factory(_libsql_factory)
    yield
    db.reset_backend()


@pytest.fixture
def conn(tmp_path):
    c = db.open_database(tmp_path / "t.db", tmp_path / "bk")
    yield c
    c.close()


@pytest.fixture
def session(conn):
    return db.get_active_session(conn)


@pytest.fixture
def alice(conn, session):
    return ppl.register(conn, "Alice", "dummy", session["id"], telegram_user_id=1001, username="alice")


@pytest.fixture
def bob(conn, session):
    return ppl.register(conn, "Bob", "school", session["id"], telegram_user_id=1002)


def report_data(indep=480, school=0, ex=2, p=15, c=15, b=15, **kw):
    d = dict(independent_minutes=indep, school_minutes=school, exercise_count=ex, exercise_names="pushups",
             physics_q=p, chemistry_q=c, biology_q=b, notes="")
    d.update(kw)
    return d


def approved_report(conn, person, session, day, **kw):
    """Admin-entered (auto-approved) report helper."""
    return reports.submit_report(conn, person, session, day, report_data(**kw), ADMIN, by_admin=True)
