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

`reverse()` and `geocode()` are the two directions across a coordinate, and
`reverse()` is the one that earns its place: it returns `in_traffic_zone`
and `in_odd_even_zone` — طرح ترافیک and زوج/فرد — which decide whether a
drive is legal this morning and which **nothing else V can reach knows**.
Google's Maps URLs model neither, as `directions_url` says at length. Read
`geocode`'s docstring before using it for anything that sounds like a place
*name*; it is a street geocoder and it fails by confidently returning the
wrong province.

**Every service here is separately enabled on the key, and they are not all
on.** Checked against Parsa's key on 2026-09-09:

    /v5/reverse            ✓   /v1/search           ✗ 485
    /v6/geocoding          ✓   /v1/distance-matrix  ✗ 485
    /v4/direction          ✓   account-inquiry/*    ✗ 485
    /v4/direction/no-traffic ✓ autocomplete, map-matching, trip — no such path
    /v5/static             ✓

So `search()` — the POI lookup this module opened with — is the one that is
*off*, which is why `_get` turns a 485 into a sentence naming the service in
Persian and pointing at the سرویس‌ها tab. The list will drift; the check that
does not drift is calling the thing and reading what comes back.

Deliberately thin and deliberately optional. With no `NESHAN_API_KEY` set,
every function here returns `None` rather than raising, and the caller
(see `mcp/server.py`) turns that into an instruction for the model to fall
back to `WebSearch`/`WebFetch` against Google Maps or Neshan's own site — a
live search still happens, just through the shell's general-purpose tools
instead of a structured API call. `None` means *not configured* and nothing
else: a service that is off, or a route Neshan refuses, raises.

**Neshan does car and motorcycle only.** There is no transit mode and no
walking mode at any key tier, so "how long by metro" is not a question this
module can answer and `route()` will not pretend otherwise — see `MODES`.
Google's Routes API does have transit, which is the one real reason to want
the `maps_provider` seam below; it needs a billing account, so it stays
unimplemented until there's a card behind it.

**And زوج/فرد cannot always be avoided, because it is a place and not a
road.** Ask `/v4/direction` for a route that avoids it into somewhere that
is *inside* it and Neshan answers "No Route Found!" — correctly, since no
such route exists. That refusal cost a real answer on 2026-09-08. The
handling is in `_neshan_route`: ask for the legal route first, and if it is
refused, relax that one restriction, say so, and use `reverse()` to name the
end that is in the zone. طرح ترافیک is never relaxed — it is not
plate-dependent, so there is nothing for Parsa to check.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Settings, settings as default_settings

SEARCH_URL = "https://api.neshan.org/v1/search"
DIRECTION_URL = "https://api.neshan.org/v4/direction"
#: Same route, priced without live traffic. The pair is the interesting
#: number: Divar → Aziz is 15 minutes of road and 24 minutes of Tehran, and
#: only the difference tells you whether to leave now or after eight.
DIRECTION_FREE_URL = "https://api.neshan.org/v4/direction/no-traffic"
#: A point to an address — and, the part that earns its place here, whether
#: that point is inside طرح ترافیک or زوج/فرد. Nothing else V can reach knows
#: that, and it is a question with a different answer every morning.
REVERSE_URL = "https://api.neshan.org/v5/reverse"
#: An address to a point. Read `geocode`'s docstring before using it for
#: anything that sounds like a place *name*.
GEOCODE_URL = "https://api.neshan.org/v6/geocoding"
#: Raster tiles for a region. Up here with the rest of them rather than down
#: beside `static_map`, so the endpoints this module talks to are one list.
STATIC_URL = "https://api.neshan.org/v5/static"

#: Neshan's own code for "this service is not on your key" — an HTTP status
#: outside the registered range, so `raise_for_status` reports it as a client
#: error and points at MDN, which has never heard of 485 either. Caught by
#: name everywhere here so the message can say what to actually do.
NOT_ON_KEY = 485

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
    #: A restriction Neshan could not route around, so it was dropped —
    #: "زوج/فرد" or "". Set rather than swallowed because the difference
    #: between "avoiding the restrictions" and "not" is the difference
    #: between a route Parsa can drive today and one he cannot, and only he
    #: knows his plate. Empty means both restrictions were honoured.
    relaxed: str = ""
    #: *Why* it had to be relaxed, when that is knowable — "Omlet Sara is
    #: inside the زوج/فرد zone". A restriction dropped with no reason given
    #: reads as V being sloppy; the real reason is usually that the
    #: destination is in the zone, in which case no route could ever have
    #: avoided it and the retry was not a workaround but the only answer.
    relaxed_why: str = ""
    #: The same route priced with no traffic at all, or 0.0 if not asked
    #: for. The pair is the useful number — 15 minutes of road and 24
    #: minutes of Tehran — and neither half says that alone.
    duration_free_s: float = 0.0

    @property
    def duration_free_min(self) -> int:
        return round(self.duration_free_s / 60)

    @property
    def traffic_min(self) -> int:
        """Minutes that are traffic rather than distance. 0 when unknown."""
        if not self.duration_free_s:
            return 0
        return max(0, round((self.duration_s - self.duration_free_s) / 60))

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
          free_flow: bool = False,
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
    return _neshan_route(origin, destination, free_flow=free_flow,
                         origin_label=origin_label,
                         dest_label=dest_label, mode=mode, settings=s)


#: Persian names for the services, so a "not enabled" message can say which
#: box to tick on the سرویس‌ها tab rather than leaving Parsa to guess from an
#: English path fragment.
_SERVICE_NAMES = {
    SEARCH_URL: "جستجو (search)",
    DIRECTION_URL: "مسیریابی (direction)",
    DIRECTION_FREE_URL: "مسیریابی بدون ترافیک (direction/no-traffic)",
    REVERSE_URL: "تبدیل نقطه به آدرس (reverse geocoding)",
    GEOCODE_URL: "تبدیل آدرس به نقطه (geocoding)",
    STATIC_URL: "نقشه استاتیک (static)",
}


def _get(url: str, params: dict, *, s: Settings) -> dict:
    """One Neshan GET, with its two non-obvious failure shapes handled.

    Neshan answers "this service is not on your key" with HTTP **485**, which
    is outside the registered status range — so `raise_for_status` calls it a
    client error and prints a link to an MDN page that has never heard of it
    either. That is what `vault_places_search` returned on 2026-09-08: a
    stack of URL-encoding and a dead documentation link, for a problem whose
    entire fix is one checkbox on platform.neshan.org.

    And a *logical* failure comes back as HTTP 200 with `status: ERROR` in
    the body, so the status line alone never means the call worked. Callers
    read the body; this only guarantees they get one.
    """
    try:
        r = httpx.get(url, params=params,
                      headers={"Api-Key": s.neshan_api_key}, timeout=20)
        if r.status_code == NOT_ON_KEY:
            name = _SERVICE_NAMES.get(url, url)
            raise MapSearchError(
                f"The Neshan key is valid but {name} is not one of the "
                f"services enabled on it. Tick it on the سرویس‌ها tab at "
                f"platform.neshan.org — nothing needs changing in V.")
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        raise MapSearchError(f"Neshan request to {url} failed: {exc}") from exc
    except ValueError as exc:
        raise MapSearchError(
            f"Neshan returned an unreadable response: {exc}") from exc


def _neshan_search(term: str, lat: float, lng: float, *,
                   settings: Settings, limit: int = 8) -> list[Hit] | None:
    s = settings
    if not s.neshan_api_key:
        return None
    data = _get(SEARCH_URL, {"term": term, "lat": lat, "lng": lng}, s=s)
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


@dataclass
class Address:
    """A point, as Neshan describes it — and as Tehran regulates it.

    The address half is ordinary reverse geocoding. The two booleans are the
    reason this is worth a tool: `in_traffic_zone` (طرح ترافیک) and
    `in_odd_even_zone` (زوج/فرد) are the difference between a drive Parsa can
    make this morning and a fine, they change by plate and by day, and no
    other source V can reach knows them. Google's Maps URLs do not, which is
    stated in `directions_url` and is why this stays Neshan's.
    """

    lat: float
    lng: float
    address: str = ""
    neighbourhood: str = ""
    city: str = ""
    state: str = ""
    county: str = ""
    route_name: str = ""
    in_traffic_zone: bool = False
    in_odd_even_zone: bool = False

    @property
    def label(self) -> str:
        """The shortest thing worth saying out loud about where this is."""
        parts = [p for p in (self.address or self.route_name,
                             self.neighbourhood, self.city) if p]
        seen, out = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return "، ".join(out[:2]) or f"{self.lat:.4f}, {self.lng:.4f}"

    @property
    def zones(self) -> str:
        """What driving here costs, in one phrase."""
        hit = [n for n, on in (("طرح ترافیک", self.in_traffic_zone),
                               ("زوج/فرد", self.in_odd_even_zone)) if on]
        return " and ".join(hit) if hit else ""


def reverse(lat: float, lng: float, *,
            settings: Settings | None = None) -> Address | None:
    """What is at a point. `None` with no key, same contract as the rest.

    Cheap and exact, and the only place V can learn whether somewhere is
    inside Tehran's driving restrictions — which is also what makes
    `_neshan_route`'s retry explainable rather than merely automatic. See
    there.
    """
    s = settings or default_settings
    if not s.neshan_api_key:
        return None
    data = _get(REVERSE_URL, {"lat": lat, "lng": lng}, s=s)
    if str(data.get("status") or "").upper() not in ("OK", ""):
        raise MapSearchError(
            f"Neshan could not describe {lat},{lng}"
            + (f" — it said: {data['message']}" if data.get("message") else "."))
    return Address(
        lat=lat, lng=lng,
        address=str(data.get("formatted_address") or ""),
        neighbourhood=str(data.get("neighbourhood") or ""),
        city=str(data.get("city") or ""),
        state=str(data.get("state") or ""),
        county=str(data.get("county") or ""),
        route_name=str(data.get("route_name") or ""),
        in_traffic_zone=bool(data.get("in_traffic_zone")),
        in_odd_even_zone=bool(data.get("in_odd_even_zone")),
    )


def geocode(address: str, *, verify: bool = True,
            settings: Settings | None = None) -> Address | None:
    """An address to a point. `None` with no key.

    **This is a street geocoder, not a place finder, and the difference is
    not subtle.** Checked against the live service on 2026-09-09:

        میدان ونک، تهران        → 35.7573, 51.4103   ✓ correct
        دانشگاه صنعتی شریف      → 29.5883, 52.5629   ✗ Fars province
        برج میلاد تهران         → 35.9180, 50.6828   ✗ 70 km off

    Given an *address* — a street, a square, with a city on it — it is right.
    Given a landmark or business *name* it answers confidently with a point
    in the wrong province, because it is matching words against a gazetteer
    rather than looking anything up. The tool that was supposed to answer
    "where is X" is `/v1/search`, and that is not enabled on this key
    (2026-09-09), so the temptation to reach for this one instead is exactly
    the failure mode to design against.

    Hence `verify`: the result is reverse-geocoded and the address Neshan
    actually landed on comes back with it, so "Sharif University" returning
    somewhere in استان فارس is *visible* in the answer rather than being a
    plausible pair of numbers. It costs one extra call and turns a silent
    wrong answer into an obvious one, which is the whole trade.
    """
    s = settings or default_settings
    if not s.neshan_api_key:
        return None
    term = (address or "").strip()
    if not term:
        raise MapSearchError("geocode needs an address to look up.")
    data = _get(GEOCODE_URL, {"address": term}, s=s)
    loc = data.get("location") or {}
    if str(data.get("status") or "").upper() not in ("OK", "") or not loc:
        raise MapSearchError(
            f"Neshan found no location for {term!r}"
            + (f" — it said: {data['message']}" if data.get("message") else "."))
    try:
        # `x` is longitude and `y` is latitude, the opposite of how every
        # `location` field in this vault is written. Getting this backwards
        # puts Tehran in the Indian Ocean, so it is unpacked by name once,
        # here, and never re-derived at a call site.
        lng, lat = float(loc["x"]), float(loc["y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapSearchError(
            f"Neshan returned a location V could not read: {loc!r}") from exc
    if not verify:
        return Address(lat=lat, lng=lng)
    found = reverse(lat, lng, settings=s)
    return found or Address(lat=lat, lng=lng)


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


def _fetch_route(origin, destination, *, mode: str, odd_even: bool,
                 s: Settings, url: str = DIRECTION_URL) -> dict:
    """One call to a direction endpoint. Returns the decoded body, error
    payload and all — a Neshan refusal is HTTP 200, so the caller looks."""
    params = {
        "type": mode,
        "origin": f"{origin[0]},{origin[1]}",
        "destination": f"{destination[0]},{destination[1]}",
        # Tehran's طرح ترافیک and زوج/فرد. On by default because a route Parsa
        # cannot legally drive is not a route, and this is the one thing no
        # other vendor offers — it is the whole reason Neshan stays primary.
        "avoidTrafficZone": "true",
    }
    if odd_even:
        params["avoidOddEvenZone"] = "true"
    return _get(url, params, s=s)


def _explain_odd_even(origin, destination, *, origin_label: str,
                      dest_label: str, s: Settings) -> str:
    """Which end of this trip is inside زوج/فرد, if either is.

    Two reverse-geocode calls, spent only when a route has already been
    refused — so the cost lands exactly where it buys something. It usually
    buys the whole answer: Omlet Sara is *in* the zone (verified 2026-09-09),
    which means no route to it could ever have avoided it, and "Neshan had no
    route that did" was a true but useless thing to tell someone. "Your
    destination is inside the zone" is the same fact, actionable.
    """
    try:
        for point, label in ((destination, dest_label), (origin, origin_label)):
            found = reverse(point[0], point[1], settings=s)
            if found is not None and found.in_odd_even_zone:
                return (f"{label or found.label} is itself inside the زوج/فرد "
                        f"zone, so no route could avoid it")
    except MapSearchError:
        pass                      # an explanation is never worth an exception
    return ""


def _neshan_route(origin: tuple[float, float], destination: tuple[float, float],
                  *, origin_label: str, dest_label: str, mode: str,
                  free_flow: bool = False,
                  settings: Settings) -> Route | None:
    """`None` means *not configured*, and nothing else.

    It used to mean three things at once — no key, no `routes` in the body,
    no legs in the route — and `vault_route` printed the first of them for
    all three. On 2026-09-08 that had V tell Parsa "routing isn't configured
    (no NESHAN_API_KEY)" about a 40-character key that was configured and
    working: Neshan had answered HTTP 200 with `{"status": "ERROR", "message":
    "No Route Found!"}`, which is a fact about the route and not about the
    installation. Saying the wrong one sends someone to edit a config file
    over a problem that is not there. A refusal now raises with Neshan's own
    words in it.

    **And the refusal was ours.** `avoidOddEvenZone=true` is what Neshan
    could not satisfy — the same two points route fine without it. Rather
    than fail, the restriction is relaxed on a retry and the route says so,
    because the honest order is: a legal route, else a route labelled as
    possibly not one, and only then nothing at all.
    """
    s = settings
    if not s.neshan_api_key:
        return None

    relaxed = relaxed_why = ""
    data = _fetch_route(origin, destination, mode=mode, odd_even=True, s=s)
    if not (data.get("routes") or []):
        retry = _fetch_route(origin, destination, mode=mode, odd_even=False, s=s)
        if retry.get("routes"):
            data, relaxed = retry, "زوج/فرد"
            relaxed_why = _explain_odd_even(
                origin, destination, origin_label=origin_label,
                dest_label=dest_label, s=s)

    routes = data.get("routes") or []
    if not routes:
        raise MapSearchError(
            "Neshan found no route between those two points"
            + (f" — it said: {data['message']}" if data.get("message") else "")
            + ". The API key is fine; this is about the route.")
    first = routes[0]
    legs = first.get("legs") or []
    if not legs:
        raise MapSearchError("Neshan returned a route with no legs in it.")

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

    # The same road with the traffic taken out. Asked for separately because
    # it is a second request, and only worth it when a person is going to
    # read the difference — `duration` alone cannot say whether 24 minutes is
    # a long way or a bad hour.
    free_s = 0.0
    if free_flow:
        try:
            quiet = _fetch_route(origin, destination, mode=mode,
                                 odd_even=not relaxed, s=s,
                                 url=DIRECTION_FREE_URL)
            for leg in ((quiet.get("routes") or [{}])[0].get("legs") or []):
                free_s += _val(leg, "duration")
        except MapSearchError:
            free_s = 0.0          # a nice-to-have never costs the route

    return Route(
        origin_label=origin_label or f"{origin[0]},{origin[1]}",
        dest_label=dest_label or f"{destination[0]},{destination[1]}",
        mode=mode, distance_m=distance_m, duration_s=duration_s,
        summary=summary, polyline=encoded,
        points=detailed or decode_polyline(encoded),
        relaxed=relaxed, relaxed_why=relaxed_why, duration_free_s=free_s,
    )


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


def mercator_latlng(x: float, y: float, zoom: int) -> tuple[float, float]:
    """The inverse of `mercator_px`: world pixels at `zoom` back to (lat, lng).
    The HUD's place map asks Neshan for a static image *centred on* each cell
    of a pixel grid, so it has to turn the grid back into coordinates."""
    import math
    world = TILE_PX * (2 ** zoom)
    lng = x / world * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / world))))
    return lat, lng


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
