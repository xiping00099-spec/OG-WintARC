# Real-browser tests (optional)

These drive a real headless Chromium against the website: opening scene (cord, keyboard, Skip, Escape, reduced motion), theme,
every page (no JavaScript errors, no failed requests, no sideways scrolling), calendar details, chart tooltips and bar geometry,
Settings, admin login + confirmation dialogs, and the mobile menu/touch targets (390×844 phone).

They use a **throw-away database with made-up participants** (created in a temp folder by `serve_demo.py`). They never read or write your real database.

```bash
cd tests/browser
npm install                         # downloads a headless Chromium (needs internet, one time)
python serve_demo.py &              # website on http://127.0.0.1:18090 (set PORT=… to change)
node e2e.js                         # prints PASS/FAIL per check; screenshots go to ./shots (override with SHOTS_DIR)
```
Needs Node 18+ and Python with the project's requirements installed. The checks run in software rendering, so they verify behaviour and layout, not frame rate on a real phone.
