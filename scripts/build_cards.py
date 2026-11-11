#!/usr/bin/env python3
"""Render the profile README cards as animated SVGs into cards/.

    python scripts/build_cards.py              animated cards  -> cards/
    STATIC=1 python scripts/build_cards.py     final frame     -> cards_static/ (+ preview.html)
    SAMPLE=1 ...                               use made-up data if data/contributions.json is missing

Cards are displayed through <img>, so they use only what works there:
an inline <style> (CSS keyframes), SMIL <animate>, and fonts subset with
pyftsubset and embedded as base64 WOFF data URIs. No scripts, no external requests.

Requires: pip install fonttools pillow
"""
import base64
import colorsys
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "cards.config.json").read_text(encoding="utf-8"))
STATIC = os.environ.get("STATIC") == "1"
OUT = ROOT / ("cards_static" if STATIC else "cards")
FONT_DIR = ROOT / "fonts"

W = 830          # README column width; cards are drawn at this scale and shown at width=100%
CHAR = 0.6       # advance width of JetBrains Mono, in em: lets us wrap and size text exactly
FONTS = {
    400: "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-Regular.ttf",
    700: "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-Bold.ttf",
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Top-to-bottom schedule (seconds): every card starts a little after the one above it.
T_HERO, T_TILES, T_CHART, T_REPOS = 0.0, 0.55, 1.1, 1.75


# --------------------------------------------------------------------------- colour

def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(x))) for x in c)


def mix(a, b, t):
    """Blend colour a toward b by t (0..1)."""
    return rgb2hex(tuple(x + (y - x) * t for x, y in zip(hex2rgb(a), hex2rgb(b))))


def lum(c):
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in hex2rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


FALLBACK = {"bg": "#0d1117", "ink": "#e6edf3", "accent": "#58a6ff"}


def sample_banner(path):
    """Pull bg / accent out of the banner image: dominant colour = bg, most vivid = accent."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((160, 160))
    q = im.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    cols = [(n, rgb2hex(pal[i * 3:i * 3 + 3])) for n, i in sorted(q.getcolors(), reverse=True)]
    bg = cols[0][1]
    rest = [(n, c) for n, c in cols[1:] if contrast(c, bg) >= 3]
    if not rest:
        rest = [(n, c) for n, c in cols[1:]]

    def vivid(c):
        _, s, v = colorsys.rgb_to_hsv(*[x / 255 for x in hex2rgb(c)])
        return s * v
    if rest and max(vivid(c) for _, c in rest) > 0.15:      # coloured banner: most vivid wins
        accent = max(rest, key=lambda nc: vivid(nc[1]) * math.sqrt(nc[0]))[1]
    elif rest:                                               # monochrome banner: highest contrast
        accent = max(rest, key=lambda nc: contrast(nc[1], bg))[1]
    else:
        accent = FALLBACK["accent"]
    return {"bg": bg, "accent": accent}


def build_palette():
    p = dict(FALLBACK)
    banner = ROOT / CFG.get("banner", "")
    source = "fallback"
    if banner.is_file():
        try:
            p.update(sample_banner(banner))
            source = f"sampled from {banner.name}"
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not sample {banner.name}: {e}", file=sys.stderr)
    else:
        print(f"warning: {banner} not found, using fallback palette", file=sys.stderr)
    if CFG.get("palette"):
        p.update(CFG["palette"])
        source += " + config overrides"
    if "ink" not in CFG.get("palette", {}):
        p["ink"] = "#f2f4f7" if lum(p["bg"]) < 0.4 else "#16181d"
    for _ in range(8):                                       # keep the accent readable on bg
        if contrast(p["accent"], p["bg"]) >= 3.5:
            break
        p["accent"] = mix(p["accent"], p["ink"], 0.15)
    p["muted"] = mix(p["ink"], p["bg"], 0.38)
    p["border"] = mix(p["bg"], p["ink"], 0.16)
    p["empty"] = mix(p["bg"], p["ink"], 0.09)
    print(f"palette ({source}): bg {p['bg']}  accent {p['accent']}  ink {p['ink']}")
    return p


# --------------------------------------------------------------------------- fonts

def ensure_fonts():
    FONT_DIR.mkdir(exist_ok=True)
    for weight, url in FONTS.items():
        f = FONT_DIR / f"JetBrainsMono-{weight}.ttf"
        if not f.exists():
            print(f"downloading {url}")
            urllib.request.urlretrieve(url, f)


def subset_b64(ttf, chars):
    """pyftsubset the font down to just these characters; return base64 WOFF."""
    exe = shutil.which("pyftsubset")
    cmd = [exe] if exe else [sys.executable, "-m", "fontTools.subset"]
    with tempfile.TemporaryDirectory() as td:
        txt, out = Path(td, "chars.txt"), Path(td, "out.woff")
        txt.write_text("".join(sorted(chars)), encoding="utf-8")
        subprocess.run(cmd + [str(ttf), f"--text-file={txt}", "--flavor=woff", "--layout-features=",
                              "--no-hinting", f"--output-file={out}"], check=True)
        return base64.b64encode(out.read_bytes()).decode()


def font_css(chars):
    faces = []
    for weight in FONTS:
        b64 = subset_b64(FONT_DIR / f"JetBrainsMono-{weight}.ttf", chars)
        faces.append("@font-face{font-family:PM;font-weight:%d;src:url(data:font/woff;base64,%s) "
                     'format("woff")}' % (weight, b64))
    return "".join(faces)


# --------------------------------------------------------------------------- svg helpers

def esc(s):
    return html.escape(str(s), quote=True)


BASE_CSS = "text{font-family:PM,ui-monospace,Menlo,Consolas,monospace}"
ANIM_CSS = (
    ".fade{animation:fade .9s ease-out var(--d,0s) both}"
    ".rise{animation:rise .8s cubic-bezier(.2,.7,.2,1) var(--d,0s) both}"
    ".pop{animation:pop .5s ease-out var(--d,0s) both;transform-box:fill-box;transform-origin:center}"
    ".grow{animation:grow 1s cubic-bezier(.2,.7,.2,1) var(--d,0s) both;transform-box:fill-box;transform-origin:left}"
    "@keyframes fade{from{opacity:0}to{opacity:1}}"
    "@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}"
    "@keyframes pop{from{opacity:0;transform:scale(.2)}to{opacity:1;transform:none}}"
    "@keyframes grow{from{transform:scaleX(0)}to{transform:none}}"
    ".still{display:none}"
    "@media (prefers-reduced-motion:reduce){"
    "*{animation:none!important}.motion{display:none}.still{display:inline}}"
)


def an(kind, delay, extra=""):
    """Class + delay hook for a CSS-animated element (nothing at all in STATIC mode)."""
    if STATIC:
        return f' class="{extra}"' if extra else ""
    return f' class="{f"{kind} {extra}".strip()}" style="--d:{delay:.2f}s"'


def dual(fn):
    """SMIL can't be switched off from CSS, so draw SMIL parts twice: an animated copy
    (.motion) and a static copy (.still); prefers-reduced-motion swaps one for the other."""
    if STATIC:
        return fn(False)
    return f'<g class="motion">{fn(True)}</g><g class="still">{fn(False)}</g>'


def txt(x, y, s, size, fill, weight=400, anchor="start", kind=None, delay=0.0):
    a = an(kind, delay) if kind else ""
    return (f'<text{a} x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>')


def frame(w, h, d0):
    return (f'<rect{an("fade", d0)} x="1" y="1" width="{w - 2}" height="{h - 2}" rx="14" '
            f'fill="{P["bg"]}" stroke="{P["border"]}" stroke-width="1.5"/>'
            f'<rect{an("grow", d0 + 0.15)} x="24" y="1" width="56" height="3" rx="1.5" fill="{P["accent"]}"/>')


def svg(w, h, body, title):
    chars = {" "}
    for t in re.findall(r">([^<>]+)<", body):
        chars.update(html.unescape(t))
    css = font_css(chars) + BASE_CSS + ("" if STATIC else ANIM_CSS)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'role="img" aria-label="{esc(title)}"><title>{esc(title)}</title>'
            f"<style>{css}</style>{body}</svg>\n")


def wrap(text, width_px, size, max_lines):
    n = max(8, int(width_px // (size * CHAR)))
    lines = textwrap.wrap(text, n) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:n - 3].rstrip() + "..."
    return lines


def fmt_day(d):
    return f"{MONTHS[d.month - 1]} {d.day}"


# --------------------------------------------------------------------------- data

def load_data():
    f = ROOT / CFG.get("data_file", "data/contributions.json")
    if f.is_file():
        return json.loads(f.read_text(encoding="utf-8"))
    if os.environ.get("SAMPLE") != "1":
        sys.exit(f"{f} not found. Run the Action (or scripts/fetch_data.py) first, "
                 "or set SAMPLE=1 to preview with made-up data.")
    print("SAMPLE=1: using made-up data. Do not commit these cards.")
    rnd = random.Random(7)
    end = date.today()
    k = end.year * 12 + end.month - 12
    d, days = date(k // 12, k % 12 + 1, 1), []
    while d <= end:
        busy = rnd.random() < (0.35 if d.weekday() < 5 else 0.12)
        days.append({"date": d.isoformat(), "count": rnd.choice([1, 1, 2, 3, 5, 8, 14]) if busy else 0})
        d += timedelta(days=1)
    return {"days": days, "repos": []}


def compute_stats(days):
    ds = [(date.fromisoformat(x["date"]), x["count"]) for x in days]
    ds.sort()
    last = ds[-1][0]
    k = last.year * 12 + last.month - 1
    keys = [((k - 11 + i) // 12, (k - 11 + i) % 12 + 1) for i in range(12)]
    monthly = {key: 0 for key in keys}
    for d, c in ds:
        if (d.year, d.month) in monthly:
            monthly[(d.year, d.month)] += c
    # current streak: today may not have a contribution yet, so start from yesterday if so
    i = len(ds) - 1
    if ds[i][1] == 0:
        i -= 1
    cur = 0
    while i >= 0 and ds[i][1] > 0:
        cur += 1
        i -= 1
    longest = run = 0
    for _, c in ds:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    busiest = max(ds, key=lambda x: (x[1], x[0]))
    return {"days": ds, "monthly": [(MONTHS[m - 1], monthly[(y, m)]) for y, m in keys],
            "total": sum(c for _, c in ds), "streak": cur, "longest": longest,
            "active": sum(1 for _, c in ds if c > 0), "span": len(ds), "busiest": busiest}


# --------------------------------------------------------------------------- cards

def portrait_uri():
    p = ROOT / CFG.get("portrait", "")
    if not p.is_file():
        return None
    from PIL import Image, ImageOps
    import io
    im = ImageOps.fit(Image.open(p).convert("RGB"), (320, 320))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def card_hero(d0):
    H, uri = 270, portrait_uri()
    ink, muted, acc = P["ink"], P["muted"], P["accent"]
    out = [frame(W, H, d0)]
    prompt = f"$ {CFG['username']}"
    cx = 48 + (len(prompt) + 0.4) * 16 * CHAR

    def cursor(anim):
        if not anim:
            return f'<rect x="{cx:.1f}" y="50" width="9" height="17" fill="{acc}"/>'
        return (f'<rect x="{cx:.1f}" y="50" width="9" height="17" fill="{acc}" opacity="0">'
                f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;.5;.5;1" dur="1.1s" '
                f'begin="{d0 + 0.5:.2f}s" repeatCount="indefinite"/></rect>')
    out += [txt(48, 64, prompt, 16, muted, kind="fade", delay=d0 + 0.2), dual(cursor),
            txt(48, 130, CFG["name"], 50, ink, 700, kind="rise", delay=d0 + 0.45)]
    for i, line in enumerate(wrap(CFG["tagline"], 570, 16, 2)):
        out.append(txt(48, 168 + i * 23, line, 16, ink, kind="rise", delay=d0 + 0.7 + i * 0.1))
    x = 48
    for i, chip in enumerate(CFG.get("chips", [])):
        w = len(chip) * 12 * CHAR + 24
        out.append(f'<g{an("rise", d0 + 1.0 + i * 0.1)}>'
                   f'<rect x="{x:.1f}" y="210" width="{w:.1f}" height="28" rx="14" fill="none" '
                   f'stroke="{P["border"]}" stroke-width="1.5"/>'
                   f'{txt(x + w / 2, 229, chip, 12, muted, anchor="middle")}</g>')
        x += w + 10
    if uri:
        cxp, cyp, r = W - 48 - 78, H / 2, 78
        out.append(f'<g{an("fade", d0 + 0.6)}><clipPath id="pc"><circle cx="{cxp}" cy="{cyp}" r="{r}"/></clipPath>'
                   f'<image href="{uri}" x="{cxp - r}" y="{cyp - r}" width="{2 * r}" height="{2 * r}" '
                   f'clip-path="url(#pc)" preserveAspectRatio="xMidYMid slice"/>'
                   f'<circle cx="{cxp}" cy="{cyp}" r="{r + 4}" fill="none" stroke="{acc}" stroke-width="2"/></g>')
    else:   # no portrait: a small field of dots that switch on diagonally
        for row in range(6):
            for col in range(9):
                o = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(col * 0.9 + row * 1.3))
                out.append(f'<circle{an("pop", d0 + 0.7 + (col + row) * 0.045)} cx="{640 + col * 18}" '
                           f'cy="{72 + row * 22}" r="3" fill="{acc}" fill-opacity="{o:.2f}"/>')
    return svg(W, H, "".join(out), f"{CFG['name']}: {CFG['tagline']}")


def card_tile(tile, d0):
    w, h = 270, 120
    out = [frame(w, h, d0),
           txt(24, 62, tile["label"], 20, P["ink"], 700, kind="rise", delay=d0 + 0.25),
           ]
    for i, line in enumerate(wrap(tile.get("sub", ""), w - 48, 12, 2)):
        out.append(txt(24, 86 + i * 16, line, 12, P["muted"], kind="rise", delay=d0 + 0.4 + i * 0.05))
    return svg(w, h, "".join(out), f"{tile['label']}: {tile.get('sub', '')}")


def card_chart(S, d0):
    H, x0, pw, base, maxh = 310, 48, 520, 252, 120
    months = S["monthly"]
    peak = max(v for _, v in months) or 1
    slot = pw / 12
    bw = 28
    top = max(range(12), key=lambda i: months[i][1])
    out = [frame(W, H, d0),
           txt(48, 62, "Contributions per month", 20, P["ink"], 700, kind="rise", delay=d0 + 0.2),
           txt(48, 84, f"{S['total']} in the last 12 months", 13, P["muted"], kind="rise", delay=d0 + 0.3),
           f'<rect{an("grow", d0 + 0.4)} x="{x0}" y="{base}" width="{pw}" height="1.5" fill="{P["border"]}"/>']

    def bars(anim):
        g = []
        for i, (m, v) in enumerate(months):
            bh = v / peak * maxh
            x = x0 + i * slot + (slot - bw) / 2
            col = P["accent"] if i == top else mix(P["bg"], P["accent"], 0.55)
            t = d0 + 0.6 + i * 0.07
            if bh < 1:
                bh = 1.5 if not anim else 1.5
            if anim:
                g.append(f'<rect x="{x:.1f}" y="{base}" width="{bw}" height="0" rx="4" fill="{col}">'
                         f'<animate attributeName="height" from="0" to="{bh:.1f}" begin="{t:.2f}s" dur=".8s" '
                         f'fill="freeze" calcMode="spline" keyTimes="0;1" keySplines=".2 .7 .2 1"/>'
                         f'<animate attributeName="y" from="{base}" to="{base - bh:.1f}" begin="{t:.2f}s" dur=".8s" '
                         f'fill="freeze" calcMode="spline" keyTimes="0;1" keySplines=".2 .7 .2 1"/></rect>')
            else:
                g.append(f'<rect x="{x:.1f}" y="{base - bh:.1f}" width="{bw}" height="{bh:.1f}" rx="4" fill="{col}"/>')
        return "".join(g)
    out.append(dual(bars))
    for i, (m, v) in enumerate(months):
        cxm = x0 + i * slot + slot / 2
        bh = max(v / peak * maxh, 1.5)
        out.append(txt(cxm, base - bh - 7, v, 11, P["ink"] if i == top else P["muted"], anchor="middle",
                       kind="fade", delay=d0 + 1.2 + i * 0.07))
        out.append(txt(cxm, base + 22, m, 11, P["muted"], anchor="middle", kind="fade", delay=d0 + 0.5))
    out.append(f'<rect{an("fade", d0 + 0.5)} x="594" y="56" width="1.5" height="210" fill="{P["border"]}"/>')
    bd, bc = S["busiest"][0], S["busiest"][1]
    stats = [(f"{S['streak']} day{'' if S['streak'] == 1 else 's'}", "current streak", f"longest {S['longest']}"),
             (f"{S['active']}", "active days", f"of the last {S['span']}"),
             (f"{bc}", "busiest day", fmt_day(bd))]
    for i, (big, label, sub) in enumerate(stats):
        y = 84 + i * 76
        t = d0 + 1.0 + i * 0.2
        out += [txt(622, y, big, 28, P["accent"], 700, kind="rise", delay=t),
                txt(622, y + 20, label, 13, P["ink"], kind="rise", delay=t + 0.05),
                txt(622, y + 37, sub, 12, P["muted"], kind="rise", delay=t + 0.1)]
    return svg(W, H, "".join(out),
               f"Contributions per month: {S['total']} total, {S['streak']} day streak, "
               f"{S['active']} active days, busiest day {fmt_day(bd)} with {bc}")


def lang_dot(r):
    c = r.get("languageColor") or P["accent"]
    return c if re.fullmatch(r"#[0-9a-fA-F]{6}", c) else P["accent"]


def star_points(cx, cy, R=6.5, r=2.8):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rad = R if i % 2 == 0 else r
        pts.append(f"{cx + rad * math.cos(a):.1f},{cy + rad * math.sin(a):.1f}")
    return " ".join(pts)


def card_repo(spec, rd, d0):
    H = 158
    name = spec["name"]
    desc = spec.get("description") or rd.get("description") or "No description yet."
    out = [frame(W, H, d0),
           txt(48, 58, name, 22, P["accent"], 700, kind="rise", delay=d0 + 0.25)]
    for i, line in enumerate(wrap(desc, W - 96, 14, 2)):
        out.append(txt(48, 86 + i * 20, line, 14, P["ink"], kind="rise", delay=d0 + 0.4 + i * 0.08))
    x, y, m = 48, 134, P["muted"]
    meta = []
    lang = rd.get("language")
    if lang:
        meta.append(f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{lang_dot(rd)}"/>')
        meta.append(txt(x + 18, y, lang, 13, m))
        x += 18 + len(lang) * 13 * CHAR + 26
    stars, forks = rd.get("stars", 0), rd.get("forks", 0)
    meta.append(f'<polygon points="{star_points(x + 6, y - 5)}" fill="none" stroke="{m}" stroke-width="1.3" '
                f'stroke-linejoin="round"/>')
    meta.append(txt(x + 18, y, stars, 13, m))
    x += 18 + len(str(stars)) * 13 * CHAR + 24
    meta.append(f'<g fill="none" stroke="{m}" stroke-width="1.3"><circle cx="{x + 2}" cy="{y - 11}" r="2"/>'
                f'<circle cx="{x + 10}" cy="{y - 11}" r="2"/><circle cx="{x + 6}" cy="{y + 1}" r="2"/>'
                f'<path d="M{x + 2} {y - 9} v2 q0 3 4 3 q4 0 4 -3 v-2 M{x + 6} {y - 4} v3"/></g>')
    meta.append(txt(x + 18, y, forks, 13, m))
    out.append(f'<g{an("rise", d0 + 0.6)}>{"".join(meta)}</g>')
    return svg(W, H, "".join(out), f"{name}: {desc}")


def card_graph(S, d0):
    H, pad = 240, 48
    pitch = (W - 2 * pad) / 53
    cell = pitch - 3
    ds = S["days"]
    first = ds[0][0]
    start = first - timedelta(days=(first.weekday() + 1) % 7)       # Sunday on/before first day
    peak = max(c for _, c in ds) or 1
    levels = [P["empty"]] + [mix(P["bg"], P["accent"], t) for t in (0.3, 0.55, 0.78, 1.0)]

    def level(c):
        if c <= 0:
            return 0
        return 1 + min(3, int(c / peak * 4 - 1e-9))
    cols, colmonth = {}, {}
    for d, c in ds:
        wk, row = (d - start).days // 7, (d.weekday() + 1) % 7
        cols.setdefault(wk, []).append(
            f'<rect x="0" y="{row * pitch:.2f}" width="{cell:.2f}" height="{cell:.2f}" rx="2.5" '
            f'fill="{levels[level(c)]}"/>')
        colmonth.setdefault(wk, d.month)
    labels, prev = [], None
    for wk in sorted(colmonth):          # label a column when its month differs from the last one
        if colmonth[wk] != prev:
            labels.append((wk, MONTHS[colmonth[wk] - 1]))
            prev = colmonth[wk]
    out = [frame(W, H, d0),
           txt(48, 56, "Contribution graph", 20, P["ink"], 700, kind="rise", delay=d0 + 0.2),
           txt(W - 48, 56, f"{S['total']} contributions", 13, P["muted"], anchor="end", kind="rise", delay=d0 + 0.3)]
    gy = 98
    shown_x = -99
    for wk, m in labels:
        lx = pad + wk * pitch
        if lx - shown_x >= 3.2 * 11 * CHAR + 14:
            out.append(txt(lx, gy - 8, m, 11, P["muted"], kind="fade", delay=d0 + 0.4))
            shown_x = lx
    for wk in sorted(cols):
        out.append(f'<g{an("rise", d0 + 0.45 + wk * 0.014)} transform="translate({pad + wk * pitch:.2f},{gy})">'
                   f'{"".join(cols[wk])}</g>')
    ly = gy + 7 * pitch + 22
    lx = W - pad - (5 * (cell + 4) + 36 + 38)
    out.append(txt(lx, ly + 9, "Less", 11, P["muted"], anchor="end", kind="fade", delay=d0 + 1.2))
    for i, c in enumerate(levels):
        out.append(f'<rect{an("fade", d0 + 1.2)} x="{lx + 8 + i * (cell + 4):.1f}" y="{ly:.1f}" '
                   f'width="{cell:.1f}" height="{cell:.1f}" rx="2.5" fill="{c}"/>')
    out.append(txt(lx + 8 + 5 * (cell + 4) + 4, ly + 9, "More", 11, P["muted"], kind="fade", delay=d0 + 1.2))
    return svg(W, H, "".join(out), f"Contribution graph: {S['total']} contributions in the last year")


# --------------------------------------------------------------------------- main

P = {}


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main():
    global P
    ensure_fonts()
    P = build_palette()
    data = load_data()
    S = compute_stats(data["days"])
    by_name = {r["name"].lower(): r for r in data.get("repos", [])}
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.svg"):
        old.unlink()

    cards = {"hero.svg": card_hero(T_HERO)}
    for i, t in enumerate(CFG["tiles"]):
        cards[f"tile-{i + 1}.svg"] = card_tile(t, T_TILES + i * 0.12)
    cards["contributions.svg"] = card_chart(S, T_CHART)
    specs = [{"name": f} if isinstance(f, str) else f for f in CFG["featured"]]
    for i, s in enumerate(specs):
        cards[f"repo-{slug(s['name'])}.svg"] = card_repo(s, by_name.get(s["name"].lower(), {}),
                                                         T_REPOS + i * 0.35)
    cards["graph.svg"] = card_graph(S, T_REPOS + len(specs) * 0.35)

    for fname, content in cards.items():
        (OUT / fname).write_text(content, encoding="utf-8")
        print(f"{OUT.name}/{fname}  {len(content) / 1024:.1f} KB")

    if STATIC:
        write_preview(specs)
        print(f"open {OUT / 'preview.html'} in a browser and screenshot it")


def write_preview(specs):
    bg = os.environ.get("PREVIEW_BG", "#0d1117")
    imgs = ['<img src="hero.svg" width="100%">',
            "".join(f'<img src="tile-{i + 1}.svg" width="32.5%"> ' for i in range(len(CFG["tiles"]))),
            '<img src="contributions.svg" width="100%">']
    imgs += [f'<img src="repo-{slug(s["name"])}.svg" width="100%">' for s in specs]
    imgs.append('<img src="graph.svg" width="100%">')
    (OUT / "preview.html").write_text(
        f'<!doctype html><meta charset="utf-8"><body style="margin:0;background:{bg}">'
        f'<div style="width:{W}px;margin:24px auto;text-align:center;line-height:0">'
        + "<br>".join(imgs).replace("<br>", '<div style="height:10px"></div>') + "</div>",
        encoding="utf-8")


if __name__ == "__main__":
    main()
