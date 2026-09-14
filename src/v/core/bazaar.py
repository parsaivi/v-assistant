"""Two Iranian marketplaces, priced in toman — the prices the vault is in.

`13 - Finance Tracker` records what Parsa *spent*. Nothing in the vault knows
what anything *costs*, which is the other half of every buying question he
actually asks: is 3 million reasonable for this, what does a used one go for,
did the thing I logged last month get cheaper. Those have no answer in a
SELECT and never will — same argument `mapsearch` opens with, one domain over.

Two sources, because they answer different questions. **Digikala** is retail:
new, one price, a seller with a rating, in stock or not. **Divar** is the
second-hand market, where the same object has a range rather than a price and
the interesting number is what people are asking in this city this week.

**Neither of these is an official, documented API, and this module should say
so wherever it is quoted from.** Digikala's `/v1/search` is what the site's
own front end calls; Divar's `/v8/postlist/w/search` is what the app calls.
Both are keyless and neither is UA-gated (checked 2026-09-09 — a bare httpx
request with no headers gets 200 from both), and both can change shape
without notice or a version bump, which is what `_rows`/`_products` are built
to survive: a field that stops existing costs that one field, not the call.
Divar's own supported platform is Kenar (kenar.divar.dev), which is a
different thing entirely — it lets you attach services *to* a listing and
does not search them, so it is not what this is.

**Everything out of here is toman.** CLAUDE.md is unambiguous that the vault
is toman throughout and never raw rial, and the two sources disagree with
each other about that, which is exactly the sort of thing to fix once, here,
rather than at each call site:

* Digikala answers in **rial**, as an integer (`selling_price: 105632700`).
  Divided by ten on the way out.
* Divar answers in **toman** already, but as a Persian-digit *string*
  (`"۱۹۹,۰۰۰,۰۰۰ تومان"`), so it is parsed rather than read.

`price_toman` is `None`, never `0`, when a listing has no number on it —
Divar's توافقی and رهن کامل, an out-of-stock Digikala page. Zero would sort
to the top of a "cheapest" list and read as free.

One shape to know about, checked on 2026-09-09: **a rental's two figures
arrive in two different fields.** ودیعه (the deposit) is Divar's
`top_description_text` and اجاره (the rent) is `middle_description_text`, so
`price_toman` on a rental is the *rent* and the deposit is on `note`. A
رهن کامل listing has a deposit and no rent at all, so its `price_toman` is
`None` while its `note` carries the only number it has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

DIGIKALA_SEARCH = "https://api.digikala.com/v1/search/"
DIGIKALA_WEB = "https://www.digikala.com"
DIVAR_SEARCH = "https://api.divar.ir/v8/postlist/w/search"
#: All 1,177 of them, keyless, in one GET. Only fetched when a city is asked
#: for that `CITIES` below doesn't already know.
DIVAR_CITIES = "https://api.divar.ir/v8/places/cities"
DIVAR_POST = "https://divar.ir/v/"

#: Sent as a courtesy, not a requirement — neither service checks it. Named
#: rather than disguised as a browser: if either of them ever wants to rate
#: limit or block this, they should be able to see who it is.
_UA = "v-assistant (personal vault assistant; +https://github.com/)"

#: The cities that cover essentially every question Parsa will ask, so the
#: common case costs no extra round trip. Anything else falls through to
#: `_resolve_city`, which fetches the full list once per process. Ids are
#: Divar's own, read from `/v8/places/cities` on 2026-09-09.
CITIES: dict[str, int] = {
    "تهران": 1, "tehran": 1,
    "کرج": 2, "karaj": 2,
    "مشهد": 3, "mashhad": 3,
    "اصفهان": 4, "isfahan": 4, "esfahan": 4,
    "تبریز": 5, "tabriz": 5,
    "شیراز": 6, "shiraz": 6,
    "اهواز": 7, "ahvaz": 7,
}

DEFAULT_CITY = "تهران"

#: Persian (U+06Fx) and Arabic-Indic (U+066x) digits both turn up in Divar's
#: strings, sometimes in the same response, along with U+066C as a thousands
#: separator. All of them normalise to ASCII before anything tries to read a
#: number out.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class BazaarError(RuntimeError):
    """A marketplace was reached and did not answer usefully — or was not
    reached at all. Either way the honest reply names the source."""


def ascii_digits(text: str) -> str:
    return (text or "").translate(_DIGITS)


def parse_toman(text: str) -> int | None:
    """`"۱۹۹,۰۰۰,۰۰۰ تومان"` -> `199000000`. `None` when there is no single
    number to read.

    The live cases that hit this are توافقی (negotiable) and رهن کامل (a
    deposit-only rental) — both are text with no figure in them at all. The
    two-number branch is the defensive one: a field that ever carries a pair
    has no single price in it, and picking either half would be wrong. The
    raw string is kept on the `Listing` regardless, so refusing to reduce it
    costs nothing.
    """
    if not text:
        return None
    numbers = re.findall(r"\d[\d,،٬\.]*", ascii_digits(text))
    if len(numbers) != 1:
        return None
    digits = re.sub(r"[^\d]", "", numbers[0])
    return int(digits) if digits else None


def format_toman(amount: int | None) -> str:
    """`199000000` -> `199,000,000 T`, the shape a ledger line is written in."""
    return f"{amount:,} T" if amount is not None else "—"


@dataclass
class Listing:
    """One thing for sale, from either source, in one shape.

    `price_text` is kept beside `price_toman` on purpose. When the parse
    declines — توافقی, a rental's two figures — the original string is still
    the answer to show, and when the parse succeeds it is the receipt for it.
    """

    title: str
    source: str                      # "digikala" | "divar"
    url: str
    price_toman: int | None = None
    price_text: str = ""
    #: Where it is (Divar: the district/city line) or who is selling it
    #: (Digikala: the marketplace seller). Different questions, same slot,
    #: because both answer "from whom".
    where: str = ""
    #: Condition, discount, rating — whatever that source says that the other
    #: one doesn't have. Rendered as-is; never parsed.
    note: str = ""
    in_stock: bool = True
    #: Divar only: this is a dealer (فروشگاه), not a person selling one thing.
    #: It changes what the price *means* — checked on 2026-09-09, a "مک بوک"
    #: search returned dealers asking ۱,۰۰۰,۰۰۰ تومان as a headline for an
    #: instalment plan next to private sellers asking 260 million for an
    #: actual laptop. Averaging across the two produces a number describing
    #: nothing, which is why `price_range` can exclude them.
    shop: bool = False


def _get(url: str, *, params: dict | None = None, json_body: dict | None = None,
         source: str, timeout: float = 20.0) -> dict:
    """One request to one marketplace, with the failure named after the shop.

    "Digikala could not be reached" is actionable and "HTTPError" is not, and
    the distinction matters more here than for a keyed service: there is no
    key to check, so a failure is either the network or the endpoint having
    moved, and the reply should let Parsa tell those apart.
    """
    try:
        headers = {"User-Agent": _UA}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            r = httpx.post(url, json=json_body, headers=headers,
                           timeout=timeout)
        else:
            r = httpx.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        raise BazaarError(
            f"{source} could not be reached ({exc}). This is an undocumented "
            f"endpoint the site itself uses, so a persistent failure may mean "
            f"it moved rather than that anything is down — say the lookup "
            f"failed rather than answering the price question from memory."
        ) from exc
    except ValueError as exc:
        raise BazaarError(
            f"{source} returned something that isn't JSON: {exc}") from exc


# --------------------------------------------------------------------------
# Digikala — retail, new, one price


def digikala(term: str, *, limit: int = 8) -> list[Listing]:
    """Search Digikala. Prices come back rial and leave here toman."""
    if not (term or "").strip():
        raise BazaarError("nothing to search for — `term` is empty.")
    data = _get(DIGIKALA_SEARCH, params={"q": term}, source="Digikala")
    body = data.get("data") or {}
    return _products(body.get("products") or [], limit)


def _products(products: list, limit: int) -> list[Listing]:
    out: list[Listing] = []
    for p in products[:limit]:
        variant = p.get("default_variant") or {}
        price = variant.get("price") or {}
        rial = price.get("selling_price")
        # `//10` and not `/10`: rial prices are always whole tomans in
        # practice, and a float here would print `10563270.0` into a note.
        toman = int(rial) // 10 if isinstance(rial, (int, float)) else None
        rating = p.get("rating") or {}
        bits = []
        if price.get("discount_percent"):
            bits.append(f"−{price['discount_percent']}%")
        # Digikala rates out of 100 and everyone reads it out of 5.
        if rating.get("rate") and rating.get("count"):
            bits.append(f"{rating['rate'] / 20:.1f}★ ({rating['count']})")
        uri = (p.get("url") or {}).get("uri") or ""
        seller = (variant.get("seller") or {}).get("title") or ""
        out.append(Listing(
            title=p.get("title_fa") or p.get("title_en") or "(untitled)",
            source="digikala",
            url=f"{DIGIKALA_WEB}{uri}" if uri else DIGIKALA_WEB,
            price_toman=toman,
            price_text=format_toman(toman),
            where=seller,
            note=" · ".join(bits),
            in_stock=p.get("status") == "marketable",
        ))
    return out


# --------------------------------------------------------------------------
# Divar — second hand, a range rather than a price


#: Filled on the first miss in `CITIES` and reused for the life of the
#: process. Not persisted: the list changes about never, and a cold start
#: paying one extra GET is cheaper than a cache file that can go stale.
_city_cache: dict[str, int] = {}


def _resolve_city(name: str) -> int:
    """A city name — Persian or slug — to Divar's id."""
    key = (name or "").strip().lower()
    if not key:
        return CITIES[DEFAULT_CITY]
    if key in CITIES:
        return CITIES[key]
    if key in _city_cache:
        return _city_cache[key]
    data = _get(DIVAR_CITIES, source="Divar")
    for city in data.get("cities") or []:
        for alias in (city.get("name"), city.get("slug"),
                      city.get("second_slug")):
            if alias:
                _city_cache[str(alias).strip().lower()] = int(city["id"])
    if key in _city_cache:
        return _city_cache[key]
    raise BazaarError(
        f"Divar has no city called {name!r}. It knows "
        f"{len(_city_cache)} of them by Persian name or slug — "
        f"'تهران' and 'tehran' both work. Ask for the right spelling rather "
        f"than searching Tehran and calling it {name!r}.")


def divar(term: str, *, city: str = DEFAULT_CITY,
          limit: int = 8) -> list[Listing]:
    """Search Divar's classifieds in one city.

    One city, not all of them, and it is required rather than optional
    because a second-hand price is a local fact — a سرچ across the country
    returns a number Parsa cannot act on and a seller he cannot drive to.

    On a rental search read `price_toman` as the *rent*, not the price of the
    flat: Divar splits ودیعه and اجاره across two fields and only the second
    is the headline. The deposit is on `note`.
    """
    if not (term or "").strip():
        raise BazaarError("nothing to search for — `term` is empty.")
    city_id = _resolve_city(city)
    body = {
        "city_ids": [str(city_id)],
        # `ROOT` is "every category" — the whole of Divar rather than a
        # section of it. Narrowing it would mean carrying Divar's category
        # tree in here and keeping it current, which is a much bigger
        # promise than this module makes.
        "search_data": {
            "form_data": {"data": {"category": {"str": {"value": "ROOT"}}}},
            "query": term,
        },
    }
    data = _get(DIVAR_SEARCH, json_body=body, source="Divar")
    return _rows(data.get("list_widgets") or [], limit)


def _rows(widgets: list, limit: int) -> list[Listing]:
    """Pull the listings out of Divar's widget stream.

    The response is a render tree, not a result set: `POST_ROW` entries carry
    the listings and everything else in it — suggestion cards, banners, the
    map count row — is chrome. Filtering on the widget type rather than
    positionally is what keeps an inserted banner from being read as a
    listing with no price.
    """
    out: list[Listing] = []
    for widget in widgets:
        if widget.get("widget_type") != "POST_ROW":
            continue
        data = widget.get("data") or {}
        payload = (data.get("action") or {}).get("payload") or {}
        token = payload.get("token") or ""
        price_text = (data.get("middle_description_text") or "").strip()
        info = payload.get("web_info") or {}
        where = (data.get("bottom_description_text") or "").strip()
        if not where:
            where = " · ".join(x for x in (info.get("district_persian"),
                                           info.get("city_persian")) if x)
        # `red_text` is the placement badge — "فروشگاه" for a dealer,
        # "نردبان شده"/"پله شده" for a listing that paid to be bumped. A
        # private seller's is empty, which is the signal `shop` reads.
        badge = (data.get("red_text") or "").strip()
        note = " · ".join(x for x in
                          ((data.get("top_description_text") or "").strip(),
                           badge) if x)
        out.append(Listing(
            title=data.get("title") or "(untitled)",
            source="divar",
            url=f"{DIVAR_POST}{token}" if token else DIVAR_POST,
            price_toman=parse_toman(price_text),
            price_text=price_text or "—",
            where=where,
            note=note,
            shop="فروشگاه" in badge,
        ))
        if len(out) >= limit:
            break
    return out


#: What a rental listing says instead of a price. Matched rather than
#: inferred from a category, because `divar()` searches `ROOT` and never sees
#: one — the words are the only signal in reach.
_RENT_WORDS = ("اجاره", "رهن", "ودیعه")


def looks_like_rental(listings: list[Listing]) -> bool:
    """Whether this result set is rentals rather than things for sale.

    Worth a function because the range means something different for each,
    and the difference is not small: checked on 2026-09-09, an
    "اجاره آپارتمان" search in Tehran returns rents from 100,000 toman
    against deposits of four *billion*. Reported as a price range with no
    caveat that reads as Tehran flats going for a hundred thousand toman,
    which is the confident-wrong answer this module exists to avoid.
    """
    hits = sum(any(w in (x.price_text + " " + x.note) for w in _RENT_WORDS)
               for x in listings)
    return bool(listings) and hits * 2 > len(listings)


def price_range(listings: list[Listing], *,
                private_only: bool = False) -> tuple[int, int] | None:
    """Cheapest and dearest of whatever actually carried a number.

    The point of a second-hand search: no single listing is "the price", and
    the two ends of what is being asked today is the closest thing to one.
    `None` when nothing in the set was priced, which is a real outcome on
    Divar and not an empty result.

    `private_only` drops the dealers, and is usually what you want for "what
    is this worth" — see `Listing.shop` for what their headline prices do to
    a range.
    """
    rows = [x for x in listings if not (private_only and x.shop)]
    prices = [x.price_toman for x in rows if x.price_toman]
    return (min(prices), max(prices)) if prices else None
