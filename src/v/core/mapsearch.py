"""Live map search — the world beyond notes V already has.

`query.places()` only ever answers from `09 - .../Locations`: places Parsa
has actually visited and written down. This module is the other half —
"what's a good café near Divar that I haven't been to" — answered from
Neshan's POI search API rather than a SELECT, because there is no local
source of truth for a café that doesn't have a note yet.

Deliberately thin and deliberately optional. With no `NESHAN_API_KEY` set,
`search()` returns `None` rather than raising, and the caller (see
`mcp/server.py`'s `vault_places_search`) turns that into an instruction for
the model to fall back to `WebSearch`/`WebFetch` against Google Maps or
Neshan's own site — a live search still happens, just through the shell's
general-purpose tools instead of a structured API call.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Settings, settings as default_settings

SEARCH_URL = "https://api.neshan.org/v1/search"


@dataclass
class Hit:
    title: str
    address: str
    category: str
    lat: float
    lng: float
    distance_km: float | None = None


class MapSearchError(RuntimeError):
    """Neshan responded, but not usefully — bad key, quota, malformed body."""


def search(term: str, lat: float, lng: float, *,
           settings: Settings | None = None, limit: int = 8) -> list[Hit] | None:
    """POI search around a point. `None` when no API key is configured —
    distinct from an empty list, which means the search ran and found
    nothing. Raises `MapSearchError` on a configured-but-failing call, since
    that's worth surfacing rather than silently reading as "no results"."""
    s = settings or default_settings
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
