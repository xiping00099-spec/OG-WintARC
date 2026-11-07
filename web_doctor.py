"""
web_doctor.py - "Dr. Arc", the small study-doctor character, as ONE reusable inline-SVG component.

    doctor("welcome", size=96)

The same markup is used everywhere; only `data-state` changes. All motion lives in web_static/app.css
(CSS animations, no libraries) and switches off automatically for visitors who prefer reduced motion.
The character only represents study progress - it never gives medical or health advice.
"""
from web_theme_base import q

STATES = {
    "welcome": "Study doctor waving hello",
    "progress": "Study doctor checking progress on a clipboard",
    "completed": "Study doctor celebrating a completed target",
    "pending": "Study doctor holding a report that is waiting for approval",
    "approved": "Study doctor giving a thumbs up for an approved report",
    "rest": "Study doctor resting beside a small desk lamp",
    "empty": "Study doctor with nothing to show yet",
    "error": "Study doctor pointing out a problem",
    "loading": "Study doctor loading",
}

_SVG = """<svg class="doctor{extra}" data-state="{state}" viewBox="0 0 120 140" width="{size}" height="{h}" {a11y} focusable="false">
<ellipse class="dr-shadow" cx="60" cy="133" rx="30" ry="4"/>
<g class="dr-p p-lamp"><circle class="lamp-glow" cx="19" cy="104" r="15"/><path class="dr-line" d="M8 130h22M19 130v-14l8-14"/><path class="lamp-shade" d="M20 98l12-6 6 11-12 6z"/></g>
<g class="dr-body">
<rect class="dr-foot" x="43" y="123" width="15" height="8" rx="4"/><rect class="dr-foot" x="62" y="123" width="15" height="8" rx="4"/>
<path class="dr-coat" d="M35 74q0-8 9-8h32q9 0 9 8l3 50q0 5-5 5H37q-5 0-5-5z"/>
<path class="dr-neck" d="M51 66l9 12 9-12z"/>
<path class="dr-fold" d="M60 78v51M49 82l-5 8M71 82l5 8"/>
<rect class="dr-pocket" x="68" y="104" width="12" height="10" rx="2.5"/>
<path class="dr-steth" d="M46 70c-3 20 3 31 16 31s19-11 15-31"/><circle class="dr-steth-end" cx="62" cy="103" r="3.6"/>
<g class="dr-arm arm-l"><rect x="27" y="72" width="10" height="31" rx="5"/><circle cx="32" cy="104" r="5"/></g>
<g class="dr-arm arm-r"><rect x="83" y="72" width="10" height="31" rx="5"/><circle cx="88" cy="104" r="5"/><g class="dr-p p-thumb"><path d="M84 96l4-12q3-1 3 3l-1 6h7q3 1 2 4l-2 8q-1 3-4 3h-9z"/></g></g>
<g class="dr-head">
<line class="dr-ant" x1="60" y1="16" x2="60" y2="8"/><circle class="dr-ant-dot" cx="60" cy="6" r="3"/>
<rect class="dr-skull" x="30" y="16" width="60" height="49" rx="21"/><circle class="dr-ear" cx="28" cy="41" r="4.5"/><circle class="dr-ear" cx="92" cy="41" r="4.5"/>
<circle class="dr-mirror" cx="76" cy="22" r="5.5"/><circle class="dr-mirror-dot" cx="76" cy="22" r="1.8"/>
<rect class="dr-face" x="38" y="27" width="44" height="29" rx="13"/>
<g class="dr-eyes"><rect class="dr-eye" x="48" y="35" width="6.5" height="10" rx="3.25"/><rect class="dr-eye" x="65.5" y="35" width="6.5" height="10" rx="3.25"/></g>
<g class="dr-eyes-rest"><path d="M47 41q4.5 4.5 9 0M64 41q4.5 4.5 9 0"/></g>
<path class="dr-mouth" d="M53 49q7 6 14 0"/><circle class="dr-mouth-o" cx="60" cy="49.5" r="2.6"/>
</g></g>
<g class="dr-p p-clip"><rect class="dr-paper" x="45" y="88" width="30" height="35" rx="4"/><rect class="dr-clipbar" x="54" y="85" width="12" height="6" rx="2.5"/><path class="dr-line" d="M51 100h18M51 106h12"/><rect class="clip-fill" x="51" y="112" width="18" height="4" rx="2"/></g>
<g class="dr-p p-timer"><circle class="dr-paper" cx="101" cy="30" r="9"/><path class="dr-line" d="M101 30V24" /><path class="timer-hand" d="M101 30l5 3"/><rect class="dr-clipbar" x="98" y="18" width="6" height="3" rx="1.5"/></g>
<g class="dr-p p-doc"><path class="dr-paper" d="M44 90h22l10 10v22H44z"/><path class="dr-line" d="M50 102h14M50 109h20M50 116h12"/></g>
<g class="dr-p p-dots"><circle cx="98" cy="40" r="2.6"/><circle cx="106" cy="40" r="2.6"/><circle cx="114" cy="40" r="2.6"/></g>
<g class="dr-p p-spark"><path class="sp s1" d="M18 26l2.4 6 6 2.4-6 2.4-2.4 6-2.4-6-6-2.4 6-2.4z"/><path class="sp s2" d="M102 20l1.8 4.5 4.5 1.8-4.5 1.8-1.8 4.5-1.8-4.5-4.5-1.8 4.5-1.8z"/><path class="sp s3" d="M106 62l1.5 3.5 3.5 1.5-3.5 1.5-1.5 3.5-1.5-3.5-3.5-1.5 3.5-1.5z"/></g>
<g class="dr-p p-check"><circle cx="100" cy="28" r="9"/><path d="M95.5 28.5l3.2 3.2 6-6.4"/></g>
<g class="dr-p p-zzz"><text x="88" y="22">z</text><text x="98" y="12">z</text></g>
<g class="dr-p p-empty"><rect x="40" y="108" width="40" height="17" rx="5"/><text class="q" x="96" y="26">?</text></g>
<g class="dr-p p-warn"><path d="M100 17l10 18H90z"/><path class="dr-warn-mark" d="M100 24v6M100 33v.1"/></g>
</svg>"""


def doctor(state: str = "welcome", size: int = 96, decorative: bool = False, extra_class: str = "") -> str:
    """Return the character as inline SVG. Use decorative=True when text next to it already says the same thing."""
    if state not in STATES:
        raise ValueError(f"unknown doctor state: {state}")
    a11y = 'aria-hidden="true"' if decorative else f'role="img" aria-label="{q(STATES[state])}"'
    return _SVG.format(state=state, size=int(size), h=int(size * 140 / 120), a11y=a11y,
                       extra=(" " + q(extra_class)) if extra_class else "")
