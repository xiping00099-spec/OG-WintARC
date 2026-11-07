"""utils.py - small helpers shared by every module (no Telegram or database code here)."""
import html
import re
import time


class ValidationError(ValueError):
    """A friendly, user-facing problem (bad input, not allowed, duplicate...)."""


def esc(text) -> str:
    """Escape text for Telegram HTML parse mode (names/notes are user controlled)."""
    return html.escape(str(text if text is not None else ""), quote=False)


def clean_text(text, max_len, label="Text", allow_empty=True) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or "")).strip()
    if not text and not allow_empty:
        raise ValidationError(f"{label} cannot be empty.")
    if len(text) > max_len:
        raise ValidationError(f"{label} is too long (max {max_len} characters).")
    return text


def fmt_minutes(minutes) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m"


def fmt_hours(minutes) -> str:
    return f"{minutes / 60:.1f}h"


_RE_COLON = re.compile(r"^(\d{1,2}):(\d{1,2})$")
_RE_HM = re.compile(r"^(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\s*(?:(\d+)\s*m?(?:in(?:ute)?s?)?)?$", re.I)
_RE_M = re.compile(r"^(\d+)\s*m(?:in(?:ute)?s?)?$", re.I)


def parse_duration(text, max_minutes=24 * 60) -> int:
    """
    '7:30', '7h30m', '7h 30', '7.5h', '450m' -> minutes.  A bare number is rejected
    (is '8' eight hours or eight minutes?) except for 0.
    """
    t = str(text).strip().lower()
    if t in ("0", "none", "no", "nil"):
        return 0
    minutes = None
    if m := _RE_COLON.match(t):
        h, mm = int(m.group(1)), int(m.group(2))
        if mm >= 60:
            raise ValidationError("Minutes must be below 60 in H:MM format (e.g. 7:30).")
        minutes = h * 60 + mm
    elif m := _RE_HM.match(t):
        minutes = round(float(m.group(1)) * 60) + (int(m.group(2)) if m.group(2) else 0)
    elif m := _RE_M.match(t):
        minutes = int(m.group(1))
    if minutes is None:
        raise ValidationError("I couldn't read that duration. Use e.g. 7:30, 7h30m, 7.5h or 450m (or 0).")
    if minutes > max_minutes:
        raise ValidationError(f"That is more than {max_minutes // 60} hours - please check the value.")
    return minutes


def parse_count(text, maximum, label="Value") -> int:
    t = str(text).strip()
    if not re.fullmatch(r"-?\d+", t):
        raise ValidationError(f"{label} must be a whole number like 0, 5 or 20.")
    n = int(t)
    if n < 0:
        raise ValidationError(f"{label} cannot be negative.")
    if n > maximum:
        raise ValidationError(f"{label} cannot be more than {maximum}.")
    return n


def chunk_text(text, limit=3800):
    """Split long text on line breaks so each piece fits in one Telegram message."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur.rstrip("\n"))
    return parts or [""]


class RateLimiter:
    """Allow one action per `seconds` per key (in memory; fine for a single bot process)."""

    def __init__(self):
        self._last = {}

    def reset(self, key):
        self._last.pop(key, None)

    def allow(self, key, seconds) -> bool:
        now = time.monotonic()
        if now - self._last.get(key, -1e9) < seconds:
            return False
        self._last[key] = now
        if len(self._last) > 5000:  # keep memory bounded
            cutoff = now - 3600
            self._last = {k: v for k, v in self._last.items() if v > cutoff}
        return True
