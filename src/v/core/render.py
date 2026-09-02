"""render.py — self-contained HTML charts and maps, opened in the browser.

V runs headless (`shells.ClaudeCodeShell`, a `claude -p` subprocess) and only
its final text ever comes back — there is no channel to push a picture into
the terminal, and there doesn't need to be: V is headed away from the
terminal eventually anyway. So a chart is not something the TUI draws — it is
something a tool builds outright.

Half-blocks were tried for the route map on 2026-09-02 and removed the same
day: at the width a transcript can spare, a Neshan tile raster reduces to a
washed-out block that reads as a rendering fault rather than a map. The
picture goes to Telegram, where it is a picture; the terminal gets the
sentence, which is the part it renders well. `mcp/server.py`'s `vault_chart` /
`vault_trend` / `vault_map` call the functions here, get back one
self-contained HTML string, `save()` it under `state_dir/renders/`, and
`open_file()` it — V's answer is just a sentence saying what it opened.

Self-contained on purpose, same as everything else in this codebase: no CDN
script, no external stylesheet, no map tile server. Inline CSS, inline JS,
inline SVG. It works with no network.

**One exception, added 2026-09-02: `place_html`.** Places drawn on a blank
grid are dots without a city under them, so it loads Neshan's OpenLayers SDK
from `static.neshan.org` and draws over live tiles. The cost is accepted
rather than hidden: the page is dead without a network and carries
`settings.neshan_web_key` in cleartext. `chart_bar_html`, `chart_line_html`
and `map_html` are unchanged and still offline-clean.

There was a `route_html` beside it, and it is gone. A local SDK page was the
wrong artifact for a route: `vault_route` now opens the route on Google Maps
(neshan.org cannot be deep-linked at all — see `mapsearch.directions_url`),
sends `route_png` to Telegram, and draws that same PNG in the terminal.

Palette, mark specs (rounded bar ends, 2px lines, ≥8px markers) and the
color-by-job rule (one hue for a single measured series, the categorical
order only when color itself carries identity — a place's category) are the
`dataviz` skill's reference instance; see its `references/palette.md` and
`references/marks-and-anatomy.md` for the derivation. This module hardcodes
that one instance rather than threading a theme through, because there is
exactly one consumer (V, for Parsa) and no brand to swap it for.
"""

from __future__ import annotations

import html as _html
import json as _json_mod
import re
import time
import webbrowser
from pathlib import Path

from .config import Settings, settings as default_settings


def _json(v) -> str:
    """JS literal for a Python value. Every dynamic value in the map
    pages goes through this — a place name is vault prose and lands
    inside a <script>, so quoting it by hand is how an apostrophe in
    "Sam's Cafe" becomes a syntax error."""
    return _json_mod.dumps(v).replace("</", "<\\/")

# --- palette (dataviz skill's reference instance) ---------------------------
SURFACE_LIGHT, SURFACE_DARK = "#fcfcfb", "#1a1a19"
INK_LIGHT, INK_DARK = "#0b0b0b", "#ffffff"
INK2_LIGHT, INK2_DARK = "#52514e", "#c3c2b7"
MUTED = "#898781"
GRID_LIGHT, GRID_DARK = "#e1e0d9", "#2c2c2a"
AXIS_LIGHT, AXIS_DARK = "#c3c2b7", "#383835"
# The eight-slot categorical order — fixed, never cycled or re-derived.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767"]
# Single-series charts (a bar-of-categories or a trend line) use one hue —
# text and axes never carry the data color, only the mark does.
ACCENT_LIGHT, ACCENT_DARK = SERIES_LIGHT[0], SERIES_DARK[0]
# Place categories, in the fixed order they take categorical slots. Six
# values, matching the `place` schema's `category` enum in `schema.py` — a
# closed, small set, unlike an open series count, so all six get their own
# slot here even though the palette's "first three validate all-pairs"
# caveat (see dataviz skill) technically only guarantees the first three
# under every possible pairing on a scatter form. Numbered pins carry
# identity too, so color is never the only channel.
PLACE_CATEGORIES = ["cafe", "restaurant", "park", "bar", "bookstore", "other"]


def _css_vars() -> str:
    light = [f"--series-{i + 1}: {c};" for i, c in enumerate(SERIES_LIGHT)]
    dark = [f"--series-{i + 1}: {c};" for i, c in enumerate(SERIES_DARK)]
    return f"""
  .viz-root {{
    color-scheme: light;
    --surface-1: {SURFACE_LIGHT};
    --text-primary: {INK_LIGHT};
    --text-secondary: {INK2_LIGHT};
    --text-muted: {MUTED};
    --grid: {GRID_LIGHT};
    --axis: {AXIS_LIGHT};
    --accent: {ACCENT_LIGHT};
    {' '.join(light)}
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1: {SURFACE_DARK};
      --text-primary: {INK_DARK};
      --text-secondary: {INK2_DARK};
      --text-muted: {MUTED};
      --grid: {GRID_DARK};
      --axis: {AXIS_DARK};
      --accent: {ACCENT_DARK};
      {' '.join(dark)}
    }}
  }}
  :root[data-theme="dark"] .viz-root {{
    color-scheme: dark;
    --surface-1: {SURFACE_DARK};
    --text-primary: {INK_DARK};
    --text-secondary: {INK2_DARK};
    --text-muted: {MUTED};
    --grid: {GRID_DARK};
    --axis: {AXIS_DARK};
    --accent: {ACCENT_DARK};
    {' '.join(dark)}
  }}"""


def _shell(title: str, body: str, *, extra_css: str = "",
           extra_head: str = "", extra_body: str = "") -> str:
    """The one HTML skeleton every chart/map renders into. `extra_head` and
    `extra_body` exist only for the SDK map pages (`route_html`,
    `place_html`), which are the sole documented exception to this module's
    no-CDN rule — every chart still renders with both empty."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
{extra_head}
<style>
{_css_vars()}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--surface-1);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .viz-root {{ padding: 28px 32px 24px; max-width: 720px; }}
  h1 {{
    font-size: 15px; font-weight: 600; color: var(--text-primary);
    margin: 0 0 4px;
  }}
  .subtitle {{ font-size: 12px; color: var(--text-muted); margin: 0 0 20px; }}
  svg text {{ fill: var(--text-secondary); font-size: 11px; }}
  svg .muted {{ fill: var(--text-muted); }}
  {extra_css}
</style>
</head>
<body>
  <div class="viz-root">
    <h1>{_html.escape(title)}</h1>
    {body}
  </div>
{extra_body}
</body>
</html>"""


def _fmt_num(v: float, unit: str = "") -> str:
    if abs(v) >= 100:
        s = f"{v:,.0f}"
    elif abs(v) >= 10:
        s = f"{v:,.1f}"
    else:
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{s} {unit}".strip()


# ---- bar chart --------------------------------------------------------
def chart_bar_html(labels: list[str], values: list[float], *,
                    title: str = "", unit: str = "") -> str:
    """One hue, direct labels: this is a magnitude compare across categories
    the axis already names, not an identity encoding — so no legend, no
    per-bar hue (dataviz skill's color-by-job rule)."""
    if not labels or not values or len(labels) != len(values):
        raise ValueError("labels and values must be the same non-empty length")
    max_v = max((abs(v) for v in values), default=0) or 1

    rows = []
    for label, v in zip(labels, values):
        pct = max(1.5, abs(v) / max_v * 100)
        tip = _html.escape(f"{label}: {_fmt_num(v, unit)}")
        rows.append(f"""
      <div class="bar-row" title="{tip}">
        <div class="bar-label">{_html.escape(str(label))}</div>
        <div class="bar-track">
          <div class="bar-fill" style="width:{pct:.2f}%"></div>
        </div>
        <div class="bar-value">{_html.escape(_fmt_num(v, unit))}</div>
      </div>""")

    css = """
  .bar-row {
    display: flex; align-items: center; gap: 10px; margin: 3px 0;
  }
  .bar-label {
    width: 34%; flex-shrink: 0; font-size: 12px; color: var(--text-secondary);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .bar-track {
    flex: 1; height: 16px; background: var(--grid); border-radius: 4px;
  }
  .bar-fill {
    height: 100%; min-width: 4px; background: var(--accent);
    border-radius: 4px;
  }
  .bar-value {
    width: auto; flex-shrink: 0; font-size: 12px; color: var(--text-primary);
    font-variant-numeric: tabular-nums; text-align: right;
  }"""
    subtitle = f'<p class="subtitle">{len(labels)} categories</p>' if not unit \
        else f'<p class="subtitle">in {_html.escape(unit)}</p>'
    body = subtitle + "\n" + "\n".join(rows)
    return _shell(title or "Chart", body, extra_css=css)


# ---- line / trend chart ------------------------------------------------
def chart_line_html(points: list[tuple[str, float]], *,
                     title: str = "", unit: str = "") -> str:
    """A single trend line. One hue, value labeled at the endpoint, a
    native-tooltip on every marker — the honest minimum of the skill's
    "ship a hover layer on line/area" rule for a static, single-view file."""
    if not points:
        raise ValueError("points must be non-empty")
    labels = [p[0] for p in points]
    values = [float(p[1]) for p in points]

    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 640, 220, 8, 8, 12, 26
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 1, hi + 1

    def x_of(i: int) -> float:
        return PAD_L + (i / max(1, len(values) - 1)) * plot_w

    def y_of(v: float) -> float:
        return PAD_T + plot_h - (v - lo) / (hi - lo) * plot_h

    pts = [(x_of(i), y_of(v)) for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
                    for i, (x, y) in enumerate(pts))

    # Baseline and up to 5 evenly spaced x-axis ticks, first/last always in.
    n_ticks = min(5, len(labels))
    tick_idx = sorted({round(i * (len(labels) - 1) / max(1, n_ticks - 1))
                       for i in range(n_ticks)})
    ticks = "\n".join(
        f'<text x="{x_of(i):.1f}" y="{H - 6}" text-anchor="middle" '
        f'class="muted">{_html.escape(str(labels[i]))}</text>'
        for i in tick_idx)
    markers = "\n".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--accent)" '
        f'stroke="var(--surface-1)" stroke-width="2">'
        f'<title>{_html.escape(f"{labels[i]}: {_fmt_num(values[i], unit)}")}'
        f'</title></circle>'
        for i, (x, y) in enumerate(pts))
    last_x, last_y = pts[-1]
    end_label = (f'<text x="{last_x:.1f}" y="{last_y - 10:.1f}" '
                f'text-anchor="end" font-weight="600" fill="var(--text-primary)">'
                f'{_html.escape(_fmt_num(values[-1], unit))}</text>')

    svg = f"""
    <svg viewBox="0 0 {W} {H}" width="100%" height="{H}" role="img"
         aria-label="{_html.escape(title)}">
      <line x1="{PAD_L}" y1="{H - PAD_B}" x2="{W - PAD_R}" y2="{H - PAD_B}"
            stroke="var(--axis)" stroke-width="1"/>
      <path d="{path}" fill="none" stroke="var(--accent)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
      {markers}
      {end_label}
      {ticks}
    </svg>"""
    subtitle = (f'<p class="subtitle">{len(values)} points'
               f'{" · " + _html.escape(unit) if unit else ""}</p>')
    return _shell(title or "Trend", subtitle + svg)


# ---- map ----------------------------------------------------------------
def map_html(points: list[dict], *, title: str = "") -> str:
    """points: [{"label", "lat", "lng", "category"?, "rating"?}, ...].

    No tile server, no CDN — an inline SVG scatter, lat/lng normalized to a
    bounding box. Numbered pins (identity survives even if a hue is
    indistinguishable) plus a legend list, since text directly on a cluster
    of pins collides.
    """
    if not points:
        raise ValueError("points must be non-empty")
    lats = [float(p["lat"]) for p in points]
    lngs = [float(p["lng"]) for p in points]
    lat_lo, lat_hi = min(lats), max(lats)
    lng_lo, lng_hi = min(lngs), max(lngs)
    if lat_lo == lat_hi:
        lat_lo, lat_hi = lat_lo - 0.01, lat_hi + 0.01
    if lng_lo == lng_hi:
        lng_lo, lng_hi = lng_lo - 0.01, lng_hi + 0.01
    # 8% padding so a point on the edge isn't drawn on the frame.
    lat_pad = (lat_hi - lat_lo) * 0.08
    lng_pad = (lng_hi - lng_lo) * 0.08
    lat_lo, lat_hi = lat_lo - lat_pad, lat_hi + lat_pad
    lng_lo, lng_hi = lng_lo - lng_pad, lng_hi + lng_pad

    W, H = 640, 400

    def xy(lat: float, lng: float) -> tuple[float, float]:
        x = (lng - lng_lo) / (lng_hi - lng_lo) * W
        y = H - (lat - lat_lo) / (lat_hi - lat_lo) * H  # north up
        return x, y

    def colour_for(category: str) -> str:
        idx = PLACE_CATEGORIES.index(category) if category in PLACE_CATEGORIES else -1
        return f"var(--series-{idx + 1})" if idx >= 0 else "var(--text-muted)"

    pins, legend = [], []
    for i, p in enumerate(points, start=1):
        x, y = xy(float(p["lat"]), float(p["lng"]))
        cat = str(p.get("category") or "")
        colour = colour_for(cat)
        label = str(p.get("label") or f"#{i}")
        rating = p.get("rating")
        tip = _html.escape(f"{i}. {label}" + (f" — {cat}" if cat else ""))
        pins.append(f"""
      <g>
        <circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="{colour}"
                stroke="var(--surface-1)" stroke-width="2">
          <title>{tip}</title>
        </circle>
        <text x="{x:.1f}" y="{y + 3.5:.1f}" text-anchor="middle"
              font-size="9" font-weight="600" fill="var(--surface-1)">{i}</text>
      </g>""")
        extra_bits = [b for b in (cat, f"★{rating}" if rating not in (None, "") else "") if b]
        text = label + (f" — {', '.join(extra_bits)}" if extra_bits else "")
        legend.append(f'<li><span class="pin-dot" style="background:{colour}">'
                      f'{i}</span> {_html.escape(text)}</li>')

    svg = f"""
    <svg viewBox="0 0 {W} {H}" width="100%" height="{H}" role="img"
         aria-label="{_html.escape(title)}"
         style="background:var(--grid); border-radius:8px;">
      {''.join(pins)}
    </svg>"""
    css = """
  .legend { list-style: none; margin: 14px 0 0; padding: 0; font-size: 12px;
            color: var(--text-secondary); }
  .legend li { margin: 3px 0; }
  .pin-dot { display: inline-flex; align-items: center; justify-content: center;
             width: 16px; height: 16px; border-radius: 50%; color: var(--surface-1);
             font-size: 9px; font-weight: 600; margin-right: 6px; }"""
    body = (f'<p class="subtitle">{len(points)} places</p>' + svg
           + f'<ul class="legend">{"".join(legend)}</ul>')
    return _shell(title or "Map", body, extra_css=css)


# ---- live map pages (the CDN exception; see module docstring) -------------
SDK_CSS = "https://static.neshan.org/sdk/openlayers/v8.1.0/neshan-sdk/v1.0.5/index.css"
SDK_JS = "https://static.neshan.org/sdk/openlayers/v8.1.0/neshan-sdk/v1.0.5/index.js"

_MAP_CSS = """
  .viz-root { max-width: 900px; }
  #map { width: 100%; height: 520px; border-radius: 8px; background: var(--grid); }
  .facts { display: flex; gap: 22px; flex-wrap: wrap; margin: 14px 0 0;
           font-size: 12px; color: var(--text-secondary); }
  .facts b { display: block; font-size: 17px; font-weight: 600;
             color: var(--text-primary); }
  .warn { margin: 14px 0 0; font-size: 12px; color: var(--text-muted); }"""


def _head(*, web_key: str) -> str:
    """The SDK tags. Emitted even with no web key: OpenLayers still draws the
    route and the pins, the tile layer just comes back empty — a visibly
    broken map beats a page that silently isn't one."""
    return (f'<link rel="stylesheet" href="{SDK_CSS}">\n'
            f'<script src="{SDK_JS}"></script>')


def _map_js(body: str) -> str:
    return f"<script>\n(function () {{\n{body}\n}})();\n</script>"


_SETUP_JS = """
  var KEY = __KEY__, CENTER = __CENTER__, ZOOM = __ZOOM__;
  if (typeof ol === 'undefined') {
    document.getElementById('map').textContent =
      'Neshan map SDK did not load — this page needs a network connection.';
    return;
  }
  var map = new ol.Map({
    target: 'map', key: KEY, maptype: 'neshan',
    view: new ol.View({ center: ol.proj.fromLonLat(CENTER), zoom: ZOOM })
  });
  function pin(lng, lat, colour, label) {
    var f = new ol.Feature({ geometry: new ol.geom.Point(ol.proj.fromLonLat([lng, lat])) });
    f.setStyle(new ol.style.Style({
      image: new ol.style.Circle({
        radius: 9, fill: new ol.style.Fill({ color: colour }),
        stroke: new ol.style.Stroke({ color: '#ffffff', width: 3 })
      }),
      text: new ol.style.Text({
        text: label, offsetY: -20, font: '600 12px system-ui',
        fill: new ol.style.Fill({ color: '#0b0b0b' }),
        stroke: new ol.style.Stroke({ color: '#ffffff', width: 4 })
      })
    }));
    return f;
  }
  var layer = new ol.source.Vector({ features: [] });
"""


def _zoom_for(span_deg: float) -> int:
    """A whole-route fit without needing the map object to measure itself.
    Coarse on purpose — OpenLayers' `fit()` would be exact, but it runs after
    the tile layer resolves and this has to be right in the written file."""
    for span, zoom in ((0.005, 16), (0.02, 14), (0.05, 13),
                       (0.12, 12), (0.3, 11), (0.8, 10)):
        if span_deg <= span:
            return zoom
    return 9


def place_html(points: list[dict], *, web_key: str = "", title: str = "") -> str:
    """The same live-tile treatment for places rather than a route — what
    `map_html` draws on a blank grid, drawn on the actual city. Kept beside
    `map_html` rather than replacing it: that one still works offline."""
    if not points:
        raise ValueError("points must be non-empty")
    lats = [float(p["lat"]) for p in points]
    lngs = [float(p["lng"]) for p in points]
    centre = [(min(lngs) + max(lngs)) / 2, (min(lats) + max(lats)) / 2]
    zoom = _zoom_for(max(max(lats) - min(lats), max(lngs) - min(lngs), 0.004))

    pins = []
    for i, p in enumerate(points):
        cat = str(p.get("category") or "")
        idx = PLACE_CATEGORIES.index(cat) if cat in PLACE_CATEGORIES else 0
        pins.append("    pin(%s, %s, %s, %s)" % (
            _json(float(p["lng"])), _json(float(p["lat"])),
            _json(SERIES_LIGHT[idx]), _json(str(p.get("label") or f"#{i + 1}"))))
    js = _SETUP_JS + "  layer.addFeatures([\n%s]);\n  map.addLayer(new ol.layer.Vector({ source: layer }));\n" % ",\n".join(pins)
    for k, v in (("__KEY__", _json(web_key)), ("__CENTER__", _json(centre)),
                 ("__ZOOM__", _json(zoom))):
        js = js.replace(k, v)

    legend = "".join(
        f'<li><span class="pin-dot" style="background:'
        f'{SERIES_LIGHT[PLACE_CATEGORIES.index(str(p.get("category")))] if str(p.get("category")) in PLACE_CATEGORIES else SERIES_LIGHT[0]}">'
        f'{i + 1}</span> {_html.escape(str(p.get("label") or ""))}</li>'
        for i, p in enumerate(points))
    css = _MAP_CSS + """
  .legend { list-style: none; margin: 14px 0 0; padding: 0; font-size: 12px;
            color: var(--text-secondary); }
  .legend li { margin: 3px 0; }
  .pin-dot { display: inline-flex; align-items: center; justify-content: center;
             width: 16px; height: 16px; border-radius: 50%; color: #fff;
             font-size: 9px; font-weight: 600; margin-right: 6px; }"""
    body = (f'<p class="subtitle">{len(points)} places</p><div id="map"></div>'
            f'<ul class="legend">{legend}</ul>')
    return _shell(title or "Places", body, extra_css=css,
                  extra_head=_head(web_key=web_key), extra_body=_map_js(js))


# ---- route as a PNG (the Telegram half) -----------------------------------
# The SDK page above is the good artifact on a desktop — pan, zoom, live
# tiles. It is useless on a phone: Telegram sends an .html as a document, so
# the answer to a question asked from the street arrives as a download. This
# renders the same route as an image instead, which Telegram shows inline
# (`telegram._PHOTO_EXTS`). Both get built; the browser opens the page and the
# phone gets the picture.
#
# Pillow is an optional extra, not a hard dependency — `pyproject` keeps the
# default install dependency-free on purpose — so a missing Pillow degrades to
# "no PNG", never to a failed answer.
PNG_W, PNG_H = 1000, 820
_PNG_PAD = 44          # px of margin the route is kept inside of


def _fit_zoom(pts: list[tuple[float, float]], width: int, height: int) -> int:
    """Largest zoom at which the whole route still fits the frame. Neshan's
    static map accepts 5-19; anything outside that is a 400, not a wide map."""
    from .mapsearch import mercator_px
    lats = [a for a, _ in pts]
    lngs = [b for _, b in pts]
    for zoom in range(19, 4, -1):
        x0, y0 = mercator_px(max(lats), min(lngs), zoom)
        x1, y1 = mercator_px(min(lats), max(lngs), zoom)
        if abs(x1 - x0) <= width - _PNG_PAD and abs(y1 - y0) <= height - _PNG_PAD:
            return zoom
    return 5


def route_png(route, *, settings: Settings | None = None,
              width: int = PNG_W, height: int = PNG_H) -> bytes | None:
    """The route drawn over Neshan's raster tiles. `None` when Pillow isn't
    installed or no key is configured — both mean "no picture", which the
    caller reports rather than treating as a failure."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    from . import mapsearch

    pts = route.points or []
    if len(pts) < 2:
        return None
    zoom = _fit_zoom(pts, width, height)
    lats = [a for a, _ in pts]
    lngs = [b for _, b in pts]
    c_lat, c_lng = (min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2

    tiles = mapsearch.static_map(c_lat, c_lng, zoom, width, height,
                                 settings=settings)
    if tiles is None:
        return None

    import io
    base = Image.open(io.BytesIO(tiles)).convert("RGBA")
    # Neshan honours the requested size exactly (no device-pixel-ratio
    # doubling), but trust the image over the request rather than the reverse.
    width, height = base.size
    cx, cy = mapsearch.mercator_px(c_lat, c_lng, zoom)

    def to_px(lat: float, lng: float) -> tuple[float, float]:
        x, y = mapsearch.mercator_px(lat, lng, zoom)
        return x - cx + width / 2, y - cy + height / 2

    xy = [to_px(a, b) for a, b in pts]

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    # A white casing under the line: the route crosses both pale streets and
    # dark green parks, and a single-colour stroke disappears into one of them.
    d.line(xy, fill=(255, 255, 255, 235), width=11, joint="curve")
    d.line(xy, fill=(*_rgb(SERIES_LIGHT[0]), 255), width=6, joint="curve")

    for point, colour in ((xy[0], SERIES_LIGHT[0]), (xy[-1], SERIES_LIGHT[1])):
        x, y = point
        d.ellipse([x - 11, y - 11, x + 11, y + 11], fill=(255, 255, 255, 255))
        d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=(*_rgb(colour), 255))

    caption = (f"{route.origin_label} → {route.dest_label}   "
               f"{route.distance_km} km · {route.duration_min} min")
    _caption(d, caption, width, height)

    out = io.BytesIO()
    Image.alpha_composite(base, overlay).convert("RGB").save(out, "PNG")
    return out.getvalue()


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _caption(draw, text: str, width: int, height: int) -> None:
    """The numbers are the answer, so they go on the picture — a screenshot
    of a route with no ETA on it is half an answer."""
    from PIL import ImageFont
    font = None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(path, 19)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad, y = 12, height - th - 34
    draw.rounded_rectangle(
        [14, y - pad, 14 + tw + pad * 2, y + th + pad],
        radius=8, fill=(255, 255, 255, 240))
    draw.text((14 + pad, y), text, font=font, fill=(11, 11, 11, 255))


# ---- save / open ---------------------------------------------------------
def save(html_str: str, *, slug: str, settings: Settings | None = None) -> Path:
    s = settings or default_settings
    out_dir = s.state_dir / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "chart"
    path = out_dir / f"{safe}-{int(time.time())}.html"
    path.write_text(html_str, encoding="utf-8")
    return path


def save_bytes(blob: bytes, *, slug: str, suffix: str = ".png",
               settings: Settings | None = None) -> Path:
    """`save()` for something already rendered to bytes — the route PNG. Same
    directory and same naming, so `renders/` stays one place to look."""
    s = settings or default_settings
    out_dir = s.state_dir / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "render"
    path = out_dir / f"{safe}-{int(time.time())}{suffix}"
    path.write_bytes(blob)
    return path


def open_url(url: str) -> None:
    """A real map in the browser. Separate from `open_file` because what gets
    opened for a route is somebody else's website, not a file this module
    wrote — and because a test wants to assert the URL without launching a
    browser to find out."""
    webbrowser.open(url)


def open_file(path: Path) -> None:
    """Kept separate from `save()` so a caller (or a test) can write the file
    without ever launching a browser."""
    webbrowser.open(path.as_uri())
