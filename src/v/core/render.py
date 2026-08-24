"""render.py — self-contained HTML charts and maps, opened in the browser.

V runs headless (`shells.ClaudeCodeShell`, a `claude -p` subprocess) and only
its final text ever comes back — there is no channel to push a picture into
the terminal, and there doesn't need to be: V is headed away from the
terminal eventually anyway. So a chart is not something the TUI draws — it is
something a tool builds outright. `mcp/server.py`'s `vault_chart` /
`vault_trend` / `vault_map` call the functions here, get back one
self-contained HTML string, `save()` it under `state_dir/renders/`, and
`open_file()` it — V's answer is just a sentence saying what it opened.

Self-contained on purpose, same as everything else in this codebase: no CDN
script, no external stylesheet, no map tile server. Inline CSS, inline JS,
inline SVG. It works with no network.

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
import re
import time
import webbrowser
from pathlib import Path

from .config import Settings, settings as default_settings

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


def _shell(title: str, body: str, *, extra_css: str = "") -> str:
    """The one HTML skeleton every chart/map renders into."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
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


# ---- save / open ---------------------------------------------------------
def save(html_str: str, *, slug: str, settings: Settings | None = None) -> Path:
    s = settings or default_settings
    out_dir = s.state_dir / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "chart"
    path = out_dir / f"{safe}-{int(time.time())}.html"
    path.write_text(html_str, encoding="utf-8")
    return path


def open_file(path: Path) -> None:
    """Kept separate from `save()` so a caller (or a test) can write the file
    without ever launching a browser."""
    webbrowser.open(path.as_uri())
