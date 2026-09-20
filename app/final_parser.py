from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from typing import Any

import requests

SERP_URL = "https://www.cian.ru/cat.php"          # desktop flats pipeline
ROOMS_URL = "https://www.cian.ru/snyat-komnatu/"  # rooms (two HTML variants!)

STATE_MARKER = '"key":"initialState","value":'
BY_ID_MARKER = '"offers":{"byId":'

# Guard for the desktop-variant fallback: real rooms total in Moscow is
# ~1000. If a rooms page reports a total this big, it's the flat SERP
# placeholder, not rooms.
ROOMS_TOTAL_LIMIT = 5000

SEARCH_PROFILES: dict[str, dict] = {
    "flat": {  # flats + studios (studios arrive with roomsCount=0)
        "deal_type": "rent",
        "engine_version": 2,
        "offer_type": "flat",
        "type": 4,
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.cian.ru/",
}


def _make_session():
    try:
        import cloudscraper
        session = cloudscraper.create_scraper()
        print("[SESSION] using cloudscraper")
    except ImportError:
        session = requests.Session()
        print("[SESSION] using plain requests")
    session.headers.update(HEADERS)
    return session


# ------------------------------------------------------- state extraction

def _extract_initial_states(html: str) -> list[dict]:
    decoder = json.JSONDecoder()
    states, pos = [], 0
    while True:
        idx = html.find(STATE_MARKER, pos)
        if idx == -1:
            break
        start = idx + len(STATE_MARKER)
        try:
            value, end = decoder.raw_decode(html, start)
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        if isinstance(value, dict):
            states.append(value)
        pos = end
    return states


def _pick_serp_state(states: list[dict]) -> dict | None:
    for st in states:
        offers = (st.get("results") or {}).get("offers")
        if isinstance(offers, list) and offers:
            return st
    return None


def _extract_offers_by_id(html: str, want_category: str = "roomRent") -> list[dict]:
    """Mobile variant: decode every '"offers":{"byId":{...}}' block, keep the
    one with the most offers of the wanted category."""
    decoder = json.JSONDecoder()
    best: list[dict] = []
    pos = 0
    while True:
        idx = html.find(BY_ID_MARKER, pos)
        if idx == -1:
            break
        start = idx + len(BY_ID_MARKER)
        try:
            by_id, end = decoder.raw_decode(html, start)
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        if isinstance(by_id, dict):
            offers = [o for o in by_id.values()
                      if isinstance(o, dict) and o.get("category") == want_category]
            if len(offers) > len(best):
                best = offers
        pos = end
    return best


# ------------------------------------------------------------- normalizing

def _address_to_string(geo: dict) -> str | None:
    comps = geo.get("address")
    if isinstance(comps, str):
        return comps or None
    if not isinstance(comps, list):
        return geo.get("userInput") or None
    parts, metro = [], None
    for c in comps:
        if not isinstance(c, dict):
            continue
        name = c.get("shortName") or c.get("title") or c.get("name")
        if not name:
            continue
        if c.get("type") == "underground":
            metro = name
        else:
            parts.append(name)
    text = ", ".join(parts) or None
    if text and metro:
        text = f"{text} ({metro})"
    return text


def _is_studio(o: dict) -> bool:
    if o.get("isStudio"):
        return True
    if o.get("roomsCount") == 0:
        return True
    for key in ("flatType", "title", "description"):
        v = o.get(key)
        if not v:
            continue
        text = json.dumps(v, ensure_ascii=False).lower()
        if "studio" in text or "студия" in text:
            return True
    return False
def _normalize_desktop_offer(o: dict, offer_type: str = "flat") -> dict[str, Any]:
    """Desktop format: results.offers entries (flats pipeline + desktop rooms)."""
    full_url = o.get("fullUrl") or ""
    url = full_url.split("?")[0]
    if url and not url.startswith("http"):
        url = f"https://www.cian.ru{url}"

    offer_id = o.get("id") or o.get("offerId")
    if not offer_id:
        m = re.search(r"/(\d{6,})", url)
        offer_id = m.group(1) if m else ""

    bargain = o.get("bargainTerms") or {}
    price = bargain.get("price") or o.get("price")

    raw_photos = o.get("photos") or o.get("minifiedPhotos") or []
    photo_urls: list[str] = []
    for p in raw_photos:
        if not isinstance(p, dict):
            continue
        u = p.get("fullUrl") or p.get("url") or p.get("minifiedUrl")
        if u:
            photo_urls.append(u)
        if len(photo_urls) >= 6:
            break

    if offer_type == "room":
        rooms = 1
        studio = False
    else:
        rooms = o.get("roomsCount")
        studio = _is_studio(o)
        if rooms is None and studio:
            rooms = 0

    return {
        "cian_id": str(offer_id or ""),
        "url": url,
        "price": price,
        "rooms": rooms,
        "address": _address_to_string(o.get("geo") or {}),
        "photo_url": photo_urls[0] if photo_urls else None,
        "photos": photo_urls,
        "is_studio": studio,
        "offer_type": offer_type,
    }


def _rooms_from_features(features: list) -> int | None:
    for f in features or []:
        if not isinstance(f, str):
            continue
        low = f.lower()
        if "студия" in low:
            return 0
        m = re.search(r"(\d+)\s*-?\s*комн", low)
        if m:
            return int(m.group(1))
    return None

def _normalize_room_offer(o: dict) -> dict[str, Any]:
    """Mobile format: offers.byId entries from snyat-komnatu."""
    href = o.get("href") or ""
    url = href.split("?")[0]
    if url and not url.startswith("http"):
        url = f"https://www.cian.ru{url}"

    offer_id = o.get("cianId") or o.get("id") or ""
    if not offer_id:
        m = re.search(r"/(\d{6,})", url)
        offer_id = m.group(1) if m else ""

    price_obj = o.get("price")
    if isinstance(price_obj, dict):
        price = price_obj.get("value") or price_obj.get("price") or price_obj.get("amount")
    else:
        price = price_obj

    geo = o.get("geo") or {}
    address = _address_to_string(geo) or geo.get("userInput") or None

    photo_urls: list[str] = []
    for m in o.get("media") or []:
        if isinstance(m, dict) and m.get("type") == "photo":
            u = m.get("fullUrl") or m.get("url") or m.get("minifiedUrl")
            if u:
                photo_urls.append(u)
            if len(photo_urls) >= 6:
                break

    rooms = _rooms_from_features(o.get("features") or [])
    if rooms is None:
        rooms = 1

    return {
        "cian_id": str(offer_id),
        "url": url,
        "price": price,
        "rooms": rooms,
        "address": address,
        "photo_url": photo_urls[0] if photo_urls else None,
        "photos": photo_urls,
        "is_studio": False,
        "offer_type": "room",
    }

def _debug_dump(o: dict, tag: str) -> None:
    """Debug dumps disabled — no files are written."""
    pass


# ------------------------------------------------------------------ rooms

def _fetch_rooms(session, region: int, page: int,
                 max_attempts: int = 3, delay: float = 2.0) -> list[dict[str, Any]]:
    """Rooms page comes in two random variants; handle both, retry a few times."""
    for attempt in range(1, max_attempts + 1):
        resp = session.get(ROOMS_URL, params={"p": page, "region": region}, timeout=30)
        html = resp.text
        print(f"[room] page={page} attempt={attempt} status={resp.status_code} "
              f"length={len(html)}")

        # --- variant 1: mobile render with offers.byId ---
        offers = _extract_offers_by_id(html, "roomRent")
        if offers:
            print(f"[room] mobile variant: {len(offers)} roomRent offers")
            if page == 1 and attempt == 1:
                _debug_dump(offers[0], "room")
            return [_normalize_room_offer(o) for o in offers]

        # --- variant 2: desktop render with initialState.results.offers ---
        state = _pick_serp_state(_extract_initial_states(html))
        if state is not None:
            res = state["results"]
            d_offers = res.get("offers") or []
            total = res.get("aggregatedOffers")
            cats = Counter(str(o.get("category")) for o in d_offers)
            print(f"[room] desktop variant: {len(d_offers)} offers, "
                  f"categories={dict(cats)}, total={total}")

            rooms = [o for o in d_offers if o.get("category") == "roomRent"]
            if rooms:
                print(f"[room] desktop variant: {len(rooms)} roomRent by category")
                if page == 1 and attempt == 1:
                    _debug_dump(rooms[0], "room_desktop")
                return [_normalize_desktop_offer(o, offer_type="room") for o in rooms]

            if d_offers and total and total <= ROOMS_TOTAL_LIMIT:
                print("[room] no roomRent category, but total fits rooms -> "
                      "treating results.offers as rooms")
                if page == 1 and attempt == 1:
                    _debug_dump(d_offers[0], "room_desktop")
                return [_normalize_desktop_offer(o, offer_type="room") for o in d_offers]

            print("[room] desktop variant without usable rooms -> saved HTML, retrying")
            with open(f"debug_rooms_desktop_p{page}_a{attempt}.html", "w", encoding="utf-8") as f:
                f.write(html)
        else:
            print("[room] neither variant recognized -> saved HTML, retrying")
            with open(f"debug_rooms_unknown_p{page}_a{attempt}.html", "w", encoding="utf-8") as f:
                f.write(html)

        if attempt < max_attempts:
            time.sleep(delay)

    print(f"[room] giving up on page {page} after {max_attempts} attempts")
    return []


# ------------------------------------------------------------------ main

def fetch_rental_listings(offer_types: tuple[str, ...] = ("flat", "room"),
                          region: int = 1, max_pages: int = 1,
                          delay: float = 2.0) -> list[dict[str, Any]]:
    session = _make_session()
    result: dict[str, dict] = {}

    # ---- flats + studios: desktop pipeline ----
    if "flat" in offer_types:
        total = None
        for page in range(1, max_pages + 1):
            params = {**SEARCH_PROFILES["flat"], "region": region, "p": page}
            resp = session.get(SERP_URL, params=params, timeout=30)
            print(f"[flat] page={page} status={resp.status_code} length={len(resp.text)}")
            resp.raise_for_status()

            state = _pick_serp_state(_extract_initial_states(resp.text))
            if state is None:
                print(f"[flat] no results.offers on page {page} — stopping flat loop")
                break

            offers = state["results"]["offers"]
            print(f"[flat] page={page}: {len(offers)} offers")

            if page == 1:
                _debug_dump(offers[0], "flat")
                n = _normalize_desktop_offer(offers[0])
                print(f"[CHECK] first flat offer normalized: {n}")

            for o in offers:
                n = _normalize_desktop_offer(o)
                if n["cian_id"]:
                    result[n["cian_id"]] = n

            if total is None:
                total = (state["results"] or {}).get("aggregatedOffers")
            if total and len(offers) and page * len(offers) >= total:
                break
            if page < max_pages:
                time.sleep(delay)

    # ---- rooms: snyat-komnatu, both HTML variants ----
    if "room" in offer_types:
        for page in range(1, max_pages + 1):
            for n in _fetch_rooms(session, region, page, delay=delay):
                if n["cian_id"]:
                    result[n["cian_id"]] = n
            if page < max_pages:
                time.sleep(delay)

    return list(result.values())


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    listings = fetch_rental_listings(offer_types=("flat", "room"), max_pages=1)
    print(f"\nFetched {len(listings)} listings total")

    by_type = Counter(l["offer_type"] for l in listings)
    print(f"By type: {dict(by_type)}")

    for item in listings[:2] + [l for l in listings if l["offer_type"] == "room"][:2]:
        print(json.dumps(item, ensure_ascii=False))