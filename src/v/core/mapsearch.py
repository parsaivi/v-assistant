"""Live map search — the world beyond notes V already has.

`query.places()` only ever answers from `09 - .../Locations`: places Parsa
has actually visited and written down. This module is the other half —
"what's a good café near Divar that I haven't been to" — answered from
Neshan's POI search API rather than a SELECT, because there is no local
source of truth for a café that doesn't have a note yet.

`route()` is the same argument one question over: "how long does it take to
get there from Divar?" has no answer in the vault either, and the honest
fallback is an estimate — which is exactly what V gave on 2026-09-02 ("20-35
minutes, the range is all traffic. Estimate, not a routing result"), because
nothing here wrapped `/v4/direction` yet.

Deliberately thin and deliberately optional. With no `NESHAN_API_KEY` set,
`search()` and `route()` return `None` rather than raising, and the caller
(see `mcp/server.py`) turns that into an instruction for the model to fall
back to `WebSearch`/`WebFetch` against Google Maps or Neshan's own site — a
live search still happens, just through the shell's general-purpose tools
instead of a structured API call.

**Neshan does car and motorcycle only.** There is no transit mode and no
walking mode at any key tier, so "how long by metro" is not a question this
module can answer and `route()` will not pretend otherwise — see `MODES`.
Google's Routes API does have transit, which is the one real reason to want
the `maps_provider` seam below; it needs a billing account, so it stays
unimplemented until there's a card behind it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Settings, settings as default_settings

SEARCH_URL = "https://api.neshan.org/v1/search"
DIRECTION_URL = "https://api.neshan.org/v4/direction"

# What `/v4/direction` accepts for `type`, and nothing else. Kept as a
# constant so the tool layer can reject "transit" with a real explanation
# rather than forwarding it and reading Neshan's 400 as a network problem.
MODES = ("car", "motorcycle")


@dataclass
class Hit:
    title: str
    address: str
    category: str
    lat: float
    lng: float
    distance_km: float | None = None


@dataclass
class Route:
    """One routing answer. `distance_m`/`duration_s` are summed across legs,
    because a route with waypoints reports per-leg and Parsa asked one
    question. `polyline` is Neshan's `overview_polyline.points`, kept encoded:
    `render.route_html` hands the string straight to OpenLayers' own
    `ol.format.Polyline`, so decoding it in Python would only be to re-encode
    it in JS. `points` is the decoded form, used for centring the map."""
    origin_label: str
    dest_label: str
    mode: str
    distance_m: float
    duration_s: float
    summary: str
    polyline: str
    points: list[tuple[float, float]]

    @property
    def distance_km(self) -> float:
        return round(self.distance_m / 1000, 1)

    @property
    def duration_min(self) -> int:
        return round(self.duration_s / 60)


class MapSearchError(RuntimeError):
    """Neshan responded, but not usefully — bad key, quota, malformed body."""


def _unsupported(s: Settings) -> MapSearchError:
    return MapSearchError(
        f"maps_provider is {s.maps_provider!r}, and only 'neshan' is "
        "implemented. Google's Places/Routes would slot in here; they need a "
        "Cloud billing account with a card on it, which is why they don't.")


def search(term: str, lat: float, lng: float, *,
           settings: Settings | None = None, limit: int = 8) -> list[Hit] | None:
    """POI search around a point. `None` when no API key is configured —
    distinct from an empty list, which means the search ran and found
    nothing. Raises `MapSearchError` on a configured-but-failing call, since
    that's worth surfacing rather than silently reading as "no results"."""
    s = settings or default_settings
    if s.maps_provider != "neshan":
        raise _unsupported(s)
    return _neshan_search(term, lat, lng, settings=s, limit=limit)


def route(origin: tuple[float, float], destination: tuple[float, float], *,
          origin_label: str = "", dest_label: str = "", mode: str = "car",
          settings: Settings | None = None) -> Route | None:
    """Directions between two points. `None` when no API key is configured,
    same contract as `search()`. `mode` is one of `MODES` — Neshan has no
    transit or walking mode, so anything else raises rather than quietly
    routing a metro trip as if it were a car."""
    s = settings or default_settings
    if s.maps_provider != "neshan":
        raise _unsupported(s)
    if mode not in MODES:
        raise MapSearchError(
            f"mode must be one of {', '.join(MODES)} — Neshan has no "
            f"{mode!r} routing at any key tier. Say so rather than "
            "substituting a car route for it.")
    return _neshan_route(origin, destination, origin_label=origin_label,
                         dest_label=dest_label, mode=mode, settings=s)


def _neshan_search(term: str, lat: float, lng: float, *,
                   settings: Settings, limit: int = 8) -> list[Hit] | None:
    s = settings
    if not s.neshan_api_key:
        return None
    try:
        r = httpx.get(SEARCH_URL, params={"term": term, "lat": lat, "lng": lng},
                      headers={"Api-Key": s.neshan_api_key}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        raise MapSearchError(f"Neshan search failed: {exc}") from exc
    except ValueError as exc:  # bad JSON
        raise MapSearchError(f"Neshan returned an unreadable response: {exc}") from exc

    items = data.get("items") or []
    out = []
    for it in items[:limit]:
        loc = it.get("location") or {}
        try:
            item_lat, item_lng = float(loc["y"]), float(loc["x"])
        except (KeyError, TypeError, ValueError):
            continue
        from .query import haversine_km  # local: keeps query.py import-free of httpx
        out.append(Hit(
            title=it.get("title") or "(untitled)",
            address=it.get("address") or it.get("region") or "",
            category=it.get("type") or it.get("category") or "",
            lat=item_lat, lng=item_lng,
            distance_km=round(haversine_km(lat, lng, item_lat, item_lng), 1),
        ))
    out.sort(key=lambda h: h.distance_km if h.distance_km is not None else 1e9)
    return out


def decode_polyline(encoded: str, *, precision: int = 5) -> list[tuple[float, float]]:
    """Google's encoded-polyline algorithm, which Neshan uses verbatim and
    OpenLayers' `ol.format.Polyline` decodes with the same default factor.
    Implemented here rather than pulled in, in keeping with the empty
    `dependencies` list — it is thirty lines and has not changed since 2005.

    Returns `(lat, lng)` pairs. Note that Neshan's *step* `start_location`
    fields are `[lng, lat]` while the polyline decodes to lat-then-lng; the
    order is the vendor's, not a choice made here."""
    points: list[tuple[float, float]] = []
    factor = float(10 ** precision)
    index = lat = lng = 0
    while index < len(encoded):
        for is_lat in (True, False):
            result, shift = 0, 0
            while True:
                if index >= len(encoded):
                    return points          # truncated payload, keep what parsed
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        points.append((lat / factor, lng / factor))
    return points


def _neshan_route(origin: tuple[float, float], destination: tuple[float, float],
                  *, origin_label: str, dest_label: str, mode: str,
                  settings: Settings) -> Route | None:
    s = settings
    if not s.neshan_api_key:
        return None
    params = {
        "type": mode,
        "origin": f"{origin[0]},{origin[1]}",
        "destination": f"{destination[0]},{destination[1]}",
        # Tehran's طرح ترافیک and زوج/فرد. On by default because a route Parsa
        # cannot legally drive is not a route, and this is the one thing no
        # other vendor offers — it is the whole reason Neshan stays primary.
        "avoidTrafficZone": "true",
        "avoidOddEvenZone": "true",
    }
    try:
        r = httpx.get(DIRECTION_URL, params=params,
                      headers={"Api-Key": s.neshan_api_key}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        raise MapSearchError(f"Neshan routing failed: {exc}") from exc
    except ValueError as exc:
        raise MapSearchError(f"Neshan returned an unreadable response: {exc}") from exc

    routes = data.get("routes") or []
    if not routes:
        return None
    first = routes[0]
    legs = first.get("legs") or []
    if not legs:
        return None

    def _val(leg: dict, key: str) -> float:
        try:
            return float((leg.get(key) or {}).get("value") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # Summed, not first-leg: with waypoints Neshan reports per leg, and the
    # question asked was about the whole trip.
    distance_m = sum(_val(leg, "distance") for leg in legs)
    duration_s = sum(_val(leg, "duration") for leg in legs)
    summary = " · ".join(str(leg.get("summary")) for leg in legs
                         if leg.get("summary"))
    encoded = str(((first.get("overview_polyline") or {}).get("points")) or "")

    # `overview_polyline` is a simplified shape — 22 points for a 7km Tehran
    # route, which visibly cuts the corners when drawn over real streets. The
    # per-step polylines are the same route at ~10x the fidelity (210 points
    # for that same trip), so prefer them and keep the overview as fallback
    # for a response that somehow has legs but no steps.
    detailed: list[tuple[float, float]] = []
    for leg in legs:
        for step in (leg.get("steps") or []):
            detailed.extend(decode_polyline(str(step.get("polyline") or "")))
    return Route(
        origin_label=origin_label or f"{origin[0]},{origin[1]}",
        dest_label=dest_label or f"{destination[0]},{destination[1]}",
        mode=mode, distance_m=distance_m, duration_s=duration_s,
        summary=summary, polyline=encoded,
        points=detailed or decode_polyline(encoded),
    )


STATIC_URL = "https://api.neshan.org/v5/static"
# Web Mercator, the projection every slippy map uses: the world is a square
# of 256·2^zoom pixels at the given zoom. Needed because Neshan's static map
# draws tiles and one marker and nothing else — the route has to be composited
# on top, and compositing means knowing where a lat/lng lands in the image.
TILE_PX = 256


def mercator_px(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """(x, y) in world pixels at `zoom`. Latitude is clamped to the Mercator
    limit rather than allowed to run to infinity at the poles; Tehran is
    nowhere near it, but a bad coordinate in a note shouldn't raise."""
    import math
    lat = max(-85.05112878, min(85.05112878, lat))
    world = TILE_PX * (2 ** zoom)
    x = (lng + 180.0) / 360.0 * world
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * world
    return x, y


def static_map(lat: float, lng: float, zoom: int, width: int, height: int, *,
               settings: Settings | None = None) -> bytes | None:
    """The raster tiles for a region, as PNG bytes. `None` with no key, same
    contract as everything else here. This is the *service* key, not the web
    key — the static endpoint is one of the boxes on the سرویس‌ها tab."""
    s = settings or default_settings
    if not s.neshan_api_key:
        return None
    try:
        r = httpx.get(STATIC_URL, params={
            "key": s.neshan_api_key, "type": "neshan", "zoom": zoom,
            "latitude": lat, "longitude": lng,
            "width": width, "height": height,
        }, headers={"Api-Key": s.neshan_api_key}, timeout=20)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise MapSearchError(f"Neshan static map failed: {exc}") from exc
    if not r.content.startswith(b"\x89PNG"):
        raise MapSearchError(
            "Neshan static map returned something that isn't a PNG — most "
            "likely 'نقشه نمایش نقشه - استاتیک' is not enabled on this key.")
    return r.content


def directions_url(origin: tuple[float, float], destination: tuple[float, float],
                   *, mode: str = "car") -> str:
    """A browser URL showing this route on a real, interactive map.

    Google Maps rather than Neshan, and not for want of trying: neshan.org is
    a SPA that rewrites its own URL and then ignores it on a cold load —
    `/maps/routing/car/origin/<latlng>/destination/<latlng>` and even a bare
    `#c<lat>-<lng>-<zoom>z` both get reset to Tehran centre when opened fresh.
    Verified in a browser on 2026-09-02; there is no deep link to build on.

    Google's does work, needs no API key and no billing — linking to the
    website was never what the card was for — and it arrives with alternate
    routes, live traffic, and a walking mode Neshan's API doesn't sell.

    Two things it does NOT give, both checked in a browser rather than
    assumed: it knows nothing of طرح ترافیک or زوج/فرد, so the numbers V
    quotes stay Neshan's and this is for looking at rather than timing; and
    Google has no transit data for Tehran at all — `travelmode=transit`
    loads the tab and answers "could not calculate transit directions". A
    metro question has no measured answer from any source here.
    """
    o = f"{origin[0]},{origin[1]}"
    d = f"{destination[0]},{destination[1]}"
    # The documented Maps URLs form (`api=1`), which is stable and needs no
    # key — not the path form, which encodes viewport state Google is free to
    # change. Neshan has no walking or transit routing, so those two modes can
    # only ever be shown, never measured.
    travel = {"car": "driving", "motorcycle": "driving",
              "walking": "walking", "transit": "transit"}.get(mode, "driving")
    return ("https://www.google.com/maps/dir/?api=1"
            f"&origin={o}&destination={d}&travelmode={travel}")
