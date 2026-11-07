# Known limitations (nothing hidden)

**Hosting and data**
* Free Render plan: database erased on restart/redeploy; service sleeps after ~15 idle minutes; bot offline while asleep (see `PERSISTENCE.md`).
* No restore command; backups must be restored by hand.
* The website's Admin page is read-only by design. Approvals, rejections, corrections, penalties and settings stay in Telegram.

**What could not be verified here**
* The bot has never been connected to real Telegram from this environment. Bot behaviour is tested through the real python-telegram-bot dispatcher with the network call replaced by a recorder.
* Nothing was deployed to Render, so the build on Render with the new files is untested. No new Python packages were added; the existing `requirements.txt` is unchanged.
* Browser tests ran only in headless Chromium with software rendering at 1280×800 and 390×844. Safari, Firefox, real iPhones/Android phones, and animation smoothness on mid-range Android were **not** tested. Animations use only opacity/transform and no blur filters to be cheap, but frame rates are unmeasured.
* The website uses modern CSS (`color-mix`, `<dialog>`, `inert`, view transitions). Very old browsers will lose some styling or the page-transition effect; the content stays usable.
* The arrow character "→" appears in two small places; in the test browser (very few fonts) it showed as a blank. Normal phones/computers have it.

**Turso**
* **Real Turso connectivity is unverified.** The build environment cannot reach Turso. All Turso-layer tests run on a local libSQL file through the same code; that proves the compatibility layer, NOT the network behaviour (latency, remote transactions, reconnects). Run `tools/turso_check.py` (see `TURSO-SETUP.md`).
* Every query becomes a network call: expect bot replies to be slower than with the local file. A slow Turso call blocks the bot while it runs.
* If Turso is down the website shows a 503 page and the bot says the database is unavailable; the app never switches to a local file. A failed write is never retried automatically; the participant is told whether it was saved and can press Try again.
* Turso free-plan limits (storage, rows read/written per month) apply; check turso.tech/pricing. Exceeding them requires upgrading.
* Off-site backups are sent as files to a Telegram group; Telegram keeps them until you delete them. The 30-day retention of local copies is temporary on Render Free.
* Backups and restore are only as good as the last successful backup; the website Admin page shows when it last succeeded.

**Design/behaviour notes**
* The creator name uses small-capital letters ("Aʀcᴀɴᴇ"); they are drawn from a bundled 3 KB font so they look the same everywhere, as larger capitals rather than tiny ones.
* Preferences (theme, opening scene, reduced motion) are stored in the visitor's browser. In private-browsing mode the opening scene may show again on each visit.
* Public pages are cached for 5 seconds, so a freshly approved report can take a few seconds to appear.
* Dr. Arc only reflects study progress; it gives no medical advice.
* Reminders and the nightly backup only run while the service is awake.
