"""
web_theme.py - the shared page frame and UI building blocks (header, opening scene, footer, stat rows, meters...).
Every page is built from these pieces so typography, spacing and motion stay consistent.
"""
import hashlib
from pathlib import Path

import config
import reports
from web_doctor import doctor
from web_theme_base import q

STATIC_DIR = Path(__file__).resolve().parent / "web_static"
STATIC_FILES = {"app.css": "text/css; charset=utf-8", "app.js": "application/javascript; charset=utf-8",
                "boot.js": "application/javascript; charset=utf-8",
                "arc-latin.woff": "font/woff"}
_version_cache = {}


def asset_version() -> str:
    """Letters-only fingerprint of the static files; changes whenever they change (cache busting)."""
    if "v" not in _version_cache:
        h = hashlib.md5()
        for name in sorted(STATIC_FILES):
            p = STATIC_DIR / name
            h.update(p.read_bytes() if p.exists() else b"")
        _version_cache["v"] = "".join(chr(97 + int(c, 16)) for c in h.hexdigest()[:8])
    return _version_cache["v"]


NAV = [("home", "Home", "Home", "/"), ("calendar", "Calendar", "Calendar", "/calendar"), ("leaderboard", "Leaderboard", "Leaderboard", "/leaderboard"),
       ("streaks", "Streaks", "Streaks", "/streaks"), ("stats", "Statistics", "Statistics", "/stats"),
       ("participants", "Participants", "Participants", "/participants"), ("admin", "Admin", "Admin", "/admin"),
       ("about", "Creator", "About the Creator", "/about")]

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' fill='%230b0b0d'/%3E"
           "%3Ccircle cx='32' cy='32' r='11' fill='%23b9f24a'/%3E%3C/svg%3E")

ICON_SUN = '<svg class="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
ICON_MOON = '<svg class="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 13.2A8.5 8.5 0 1 1 10.8 3a6.8 6.8 0 0 0 10.2 10.2z"/></svg>'
ICON_GEAR = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>'
ICON_MENU = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h16M4 16h16"/></svg>'


def human_range(session) -> str:
    s, e = reports.session_range(session)
    return f"{s.day} {s:%B %Y} – {e.day} {e:%B %Y}"


# ------------------------------------------------------------------ opening scene
def intro_scene(session) -> str:
    if session is not None:
        brand = (f'<div class="b1">Winter Arc</div><div class="b2">Session {int(session["session_number"]):02d} · {q(session["group_name"])}</div>'
                 f'<div class="b3">{q(human_range(session))}</div>')
    else:
        brand = '<div class="b1">Winter Arc</div>'
    return f"""<div id="intro" class="intro" hidden role="dialog" aria-modal="true" aria-labelledby="intro-quote">
<div class="ambient"></div>
<button type="button" class="btn ghost small intro-skip" data-intro-skip>Skip intro</button>
<div class="intro-copy"><p class="quote" id="intro-quote">Every great transformation begins with a single decision.</p><p class="hint">Pull the cord to begin.</p></div>
<div class="intro-brand" aria-hidden="false">{brand}</div>
<svg class="scene" viewBox="0 0 800 520" preserveAspectRatio="xMidYMax meet" aria-hidden="false" focusable="false">
<defs>
<linearGradient id="coneGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffd9a0" stop-opacity=".62"/><stop offset="1" stop-color="#ffb454" stop-opacity="0"/></linearGradient>
<radialGradient id="poolGrad"><stop offset="0" stop-color="#ffc77a" stop-opacity=".55"/><stop offset="1" stop-color="#ffc77a" stop-opacity="0"/></radialGradient>
<radialGradient id="haloGrad"><stop offset="0" stop-color="#ffcf8a" stop-opacity=".6"/><stop offset="1" stop-color="#ffcf8a" stop-opacity="0"/></radialGradient>
</defs>
<polygon class="cone" points="548,244 638,244 742,408 438,408"/>
<ellipse class="pool" cx="590" cy="410" rx="215" ry="24"/>
<circle class="halo" cx="592" cy="240" r="120"/>
<rect class="sc-desk" x="-1600" y="404" width="4000" height="400"/><rect class="sc-edge" x="-1600" y="404" width="4000" height="3"/>
<g><rect class="sc-book" x="176" y="388" width="176" height="16" rx="3"/><rect class="sc-item" x="196" y="373" width="132" height="15" rx="3"/><path d="M204 380h96" stroke="#4a4a52" stroke-width="2" stroke-linecap="round"/></g>
<g><rect class="sc-mug" x="400" y="362" width="42" height="42" rx="9"/><path class="sc-mug" d="M442 372h7a8 8 0 0 1 0 22h-7" fill="none"/><path class="sc-steam" d="M414 352q-6-8 0-14M426 352q-6-8 0-14"/></g>
<path d="M260 398l110-10" stroke="#5a5a62" stroke-width="3" stroke-linecap="round" opacity=".0"/>
<path class="lamp-line" d="M545 404L500 300L592 196"/><ellipse class="lamp-base" cx="545" cy="408" rx="56" ry="9"/>
<circle class="lamp-joint" cx="500" cy="300" r="8"/><circle class="lamp-joint" cx="592" cy="196" r="7"/>
<path class="lamp-shade" d="M538 244Q546 190 592 190Q638 190 646 244Z"/><rect class="lamp-shade" x="534" y="241" width="116" height="6" rx="3"/><ellipse class="bulb" cx="592" cy="247" rx="15" ry="7"/>
<g class="cord idle" role="button" tabindex="0" aria-label="Pull the cord to turn on the lamp">
<circle class="hit" cx="548" cy="296" r="38"/><rect class="string" x="546.8" y="247" width="2.4" height="70" rx="1.2"/><circle class="bead" cx="548" cy="322" r="7.5"/><circle class="ring" cx="548" cy="322" r="15"/></g>
</svg>
<div class="intro-actions"><button type="button" class="btn accent" data-intro-enter>Enter Winter Arc</button></div>
<p class="intro-live sr-only" aria-live="polite"></p>
</div>"""


# ------------------------------------------------------------------ page frame
def layout(title, body, active, session=None, url=lambda p, **k: p, sessions=()):
    v = asset_version()
    cur = ' aria-current="page"'
    links = "".join(f'<a href="{q(url(p))}" style="--n:{i}"{cur if k == active else ""}><span class="short">{short}</span><span class="long">{long}</span></a>'
                    for i, (k, short, long, p) in enumerate(NAV))
    sessions_menu = ""
    if len(sessions) > 1:
        items = "".join(f'<a href="/?s={s["id"]}">Session {int(s["session_number"]):02d}{" · archived" if s["archived"] else ""}</a>' for s in sessions)
        sessions_menu = f'<details class="menu"><summary class="icon-btn" aria-label="Switch session"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10"/></svg></summary><div class="menu-panel">{items}</div></details>'
    creator = config.CREATOR
    return f"""<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#fbfbf9"><meta name="color-scheme" content="light dark">
<title>{q(title)} · {q(creator["project"])}</title><link rel="icon" href="{FAVICON}">
<link rel="stylesheet" href="/static/app.css?v={v}"><script src="/static/boot.js?v={v}"></script><script src="/static/app.js?v={v}" defer></script></head>
<body><a class="skip" href="#main">Skip to content</a>
{intro_scene(session)}
<header class="site-header"><div class="bar"><a class="brand" href="{q(url('/'))}" aria-label="Winter Arc home"><i></i>{q(creator["project"])}</a>
<nav class="nav" id="site-nav" aria-label="Primary"><span class="nav-ind" aria-hidden="true"></span>{links}</nav>
<div class="tools">{sessions_menu}<a class="icon-btn" href="{q(url('/settings'))}" aria-label="Settings">{ICON_GEAR}</a>
<button type="button" class="icon-btn theme-btn" aria-label="Switch between light and dark theme">{ICON_SUN}{ICON_MOON}</button>
<button type="button" class="icon-btn menu-btn" aria-label="Menu" aria-expanded="false" aria-controls="site-nav">{ICON_MENU}</button></div></div></header>
<main id="main" tabindex="-1">{body}</main>
<footer class="site-footer"><div class="in"><p class="credit">Created by <b class="creator-name">{q(creator["name"])}</b> · <a href="{q(config.creator_telegram_url())}" rel="noopener" target="_blank">@{q(creator["telegram_username"])}</a></p>
<div class="links"><a href="{q(url('/about'))}">About the Creator</a><a href="{q(url('/settings'))}">Settings</a></div></div></footer>
<div class="toasts" role="status" aria-live="polite"></div>
<div class="busy" role="status" aria-live="polite">{doctor("loading", 34, decorative=True)}<span>Loading…</span></div>
<dialog id="confirm" class="dlg" aria-labelledby="confirm-title"><form method="dialog"><h2 id="confirm-title">Are you sure?</h2><p></p>
<div class="btn-row"><button class="btn ghost" value="cancel">Cancel</button><button class="btn" value="ok">Confirm</button></div></form></dialog>
</body></html>"""


# ------------------------------------------------------------------ building blocks
def count(value, dec=0, suffix="") -> str:
    """Number that counts up when it scrolls into view (the final value is already in the HTML)."""
    text = f"{value:.{dec}f}"
    if suffix == "<small>days</small>" and value == 1:
        suffix = "<small>day</small>"
    return f'<span class="count num" data-to="{value:.{dec}f}" data-dec="{dec}">{text}</span>{suffix}'


def stat_item(label, value_html, note="") -> str:
    return f'<div class="stat"><div class="k">{q(label)}</div><div class="v">{value_html}</div><div class="n">{q(note)}</div></div>'


def stats_row(items) -> str:
    return '<div class="stats stagger">' + "".join(items) + "</div>"


def meter(pct) -> str:
    pct = max(0, min(100, pct))
    return f'<div class="meter" role="presentation" style="--w:{pct:.0f}%"><i></i></div>'


def empty_state(title, text, state="empty", action="") -> str:
    return f'<div class="empty card soft reveal">{doctor(state, 88)}<h2 class="h2">{q(title)}</h2><p>{q(text)}</p>{action}</div>'


def page_head(eyebrow, title, lead="", state=None) -> str:
    art = doctor(state, 84, decorative=True) if state else ""
    lead_html = f'<p class="lead" style="margin-top:12px">{q(lead)}</p>' if lead else ""
    return (f'<div class="page-head reveal"><div><div class="eyebrow">{q(eyebrow)}</div><h1 class="h1" style="margin-top:6px">{q(title)}</h1>{lead_html}</div>{art}</div>')


def initials(name) -> str:
    parts = [p for p in str(name).replace("(", " ").replace(")", " ").split() if p]
    if not parts:
        return "?"
    letters = parts[0][0] + (parts[1][0] if len(parts) > 1 else "")
    return letters.upper()


def status_dot(status) -> str:
    return f'<span class="dot {q(status)}" aria-hidden="true"></span>'
