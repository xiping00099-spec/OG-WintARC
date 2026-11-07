# FINAL CHECKLIST — Turso integration release

Nothing was deployed, pushed to GitHub, or changed on Render. No live database was touched.

## What was run (actual results)
| Check | Result |
|---|---|
| `tests/test_dbconn.py` (21 tests: row compatibility, constraint errors, read retries/reconnect, writes never retried, interrupted transaction, ambiguous COMMIT → no duplicate report, no fallback to SQLite, wrong credentials, 6-thread transaction isolation, simultaneous submissions/penalties/confirmations, read-only website view on the shared connection, backup validity, restore into a separate test database, unsafe-restore refusals + all-or-nothing, off-site backup job incl. retry/retention/restorable file, secrets redaction, config validation, no `await` inside transactions, "Query is too old", honest failed-save messages + Try again, bot/website/scheduler share one connection) | **21 passed** |
| Essential suites on plain SQLite (`test_database`, `test_reports`, `test_integration`, `test_e2e_handlers`, `test_web`) | **95 passed** |
| The same 95 tests through the Turso compatibility layer (a **local libSQL file**) | **95 passed** |
| Real process start with Turso configured but unreachable (fake credentials) | Retried 7 times over ~35 s, exit code 5, **0 local database files created**, neither token in the log |
| **Clean install of the packaged ZIP** (new virtual environment, `pip install -r requirements-dev.txt`, `pip check` = no conflicts, libsql 0.1.11): the 21 new tests + the 95 essential tests | **116 passed**; website started from the unzipped copy: `/healthz`, `/`, `/about`, `/static/app.css` = 200 |
| Half a Turso setup (URL without token) | Refused at startup with a clear message |

## What was NOT run in this release (by your instruction)
The full Python suite, the website-design tests and the real-browser suite were not repeated. Their last results (before the Turso work): 148 passed and 132/132 browser checks. The redesign files were not modified except `web.py` (database access, 503 page, Admin storage panel text).

## Verified vs not verified
* VERIFIED: the compatibility layer, locking, failure handling, backup/restore logic and bot behaviour — on a **local libSQL file**.
* **NOT VERIFIED: any real network connection to Turso.** The build environment cannot reach Turso. A local libSQL file does not prove remote latency, remote transaction behaviour or reconnects. `tools/turso_check.py` exists to prove exactly that — it is the one manual test still required.
* Not verified: a Render build with the new `libsql` package; Telegram delivery of a backup to a real group.

## Requirement checklist
- [x] Website, bot, admin panel and scheduler use ONE shared database connection (Turso when configured)
- [x] Whole transactions are protected against concurrency (lock held for the entire transaction; tested with 6 threads)
- [x] Connection loss: reads retried, writes never blindly retried, interrupted transactions discarded, clear errors
- [x] No silent fallback to local SQLite (process-level test)
- [x] Failed report submissions: participant is told saved / NOT saved / could not confirm; Try again keeps the answers; no duplicates
- [x] Off-site backups to a private Telegram group (or admins), catch-up after sleep, retry, 30-copy local retention, restorable files (tested)
- [x] Restore tool refuses to overwrite existing data without `--force` (snapshot first), validates the file, all-or-nothing
- [x] "Query is too old" no longer aborts the action
- [x] Secrets are redacted from logs; Turso variables live only in Render's Environment
- [x] Schema, scoring, penalties, streaks, permissions, bot identity, commands and website design unchanged

## Your remaining manual steps
1. Create a Turso **test** database + token; run `python tools/turso_check.py` (see `TURSO-SETUP.md`). It must print ALL REQUIRED CHECKS PASSED.
2. Create the private backup group and `BACKUP_CHAT_ID` (`BACKUP-RESTORE.md`); test with `/backup group`.
3. Create the real Turso database + token; add `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `BACKUP_CHAT_ID` in Render → Environment; deploy (upload to GitHub); confirm the log says `Turso (…)`; restart the service and confirm data survived.
