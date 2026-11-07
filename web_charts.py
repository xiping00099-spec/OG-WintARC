"""web_charts.py - small, dependency-free SVG charts (bars, lines). Every number passed in is real data."""
import math

from web_theme_base import q


def nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def _geometry(n, w, h, pad=(10, 6, 24, 38)):
    top, right, bottom, left = pad
    return top, right, bottom, left, w - left - right, h - top - bottom


def _grid(vmax, fmt, w, h, geo, steps=4):
    top, right, bottom, left, pw, ph = geo
    out = []
    for i in range(steps + 1):
        v = vmax * i / steps
        y = top + ph - ph * i / steps
        out.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="axis" x="{left - 8}" y="{y + 3.5:.1f}" text-anchor="end">{q(fmt(v))}</text>')
    return "".join(out)


def _xlabels(labels, n, geo, h, every):
    top, right, bottom, left, pw, ph = geo
    out = []
    for i, lab in enumerate(labels):
        if i % every == 0 or i == n - 1 and (n - 1) % every >= every // 2:
            x = left + pw * (i + 0.5) / n
            out.append(f'<text class="axis" x="{x:.1f}" y="{h - 6}" text-anchor="middle">{q(lab)}</text>')
    return "".join(out)


def bar_chart(values, labels, tips, aria, fmt=lambda v: f"{v:g}", w=640, h=200, every=None):
    n = len(values)
    geo = _geometry(n, w, h)
    top, right, bottom, left, pw, ph = geo
    vmax = nice_max(max(values, default=0))
    slot = pw / max(n, 1)
    bw = max(3.0, min(26.0, slot * 0.62))
    bars = []
    for i, (v, tip) in enumerate(zip(values, tips)):
        bh = ph * v / vmax if v > 0 else 0
        x = left + slot * i + (slot - bw) / 2
        if v > 0:
            bars.append(f'<rect class="cbar" style="--i:{i}" x="{x:.1f}" y="{top + ph - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="{min(4, bw / 2):.1f}" '
                        f'data-tip="{q(tip)}" tabindex="0" aria-label="{q(tip)}"/>')
        else:
            bars.append(f'<rect class="cbar zero" x="{x:.1f}" y="{top + ph - 2:.1f}" width="{bw:.1f}" height="2" rx="1" data-tip="{q(tip)}"/>')
    every = every or max(1, round(n / 8))
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{q(aria)}" preserveAspectRatio="xMidYMid meet">'
            f'{_grid(vmax, fmt, w, h, geo)}{"".join(bars)}{_xlabels(labels, n, geo, h, every)}</svg>')


def line_chart(values, labels, tips, aria, fmt=lambda v: f"{v:g}", w=640, h=200, every=None, vmin=0):
    n = len(values)
    geo = _geometry(n, w, h)
    top, right, bottom, left, pw, ph = geo
    vmax = nice_max(max(values, default=0))
    vmin = min(vmin, min(values, default=0))
    span = (vmax - vmin) or 1
    pts = [(left + pw * (i + 0.5) / n, top + ph - ph * (v - vmin) / span) for i, v in enumerate(values)]
    if not pts:
        return ""
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    area = d + f" L{pts[-1][0]:.1f} {top + ph:.1f} L{pts[0][0]:.1f} {top + ph:.1f} Z"
    dots = "".join(f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="11" data-tip="{q(t)}" tabindex="0" aria-label="{q(t)}"/><circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3.2"/>'
                   for (x, y), t in zip(pts, tips))
    every = every or max(1, round(n / 8))
    grid = _grid(vmax, fmt, w, h, geo)
    if vmin < 0:  # keep the grid honest when values go negative: relabel by value range
        grid = "".join(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{top + ph - ph * i / 4:.1f}" y2="{top + ph - ph * i / 4:.1f}"/>'
                       f'<text class="axis" x="{left - 8}" y="{top + ph - ph * i / 4 + 3.5:.1f}" text-anchor="end">{q(fmt(vmin + span * i / 4))}</text>' for i in range(5))
    return (f'<svg class="chart line" viewBox="0 0 {w} {h}" role="img" aria-label="{q(aria)}" preserveAspectRatio="xMidYMid meet">'
            f'{grid}<path class="area" d="{area}"/><path class="stroke" pathLength="1" d="{d}"/>{dots}{_xlabels(labels, n, geo, h, every)}</svg>')
