import sys
from collections import Counter

import cloudscraper

sys.path.insert(0, r"D:\VS_Code\moscow_room_bot")
from app.parser import _extract_initial_states

URL = sys.argv[1] if len(sys.argv) > 1 else "PASTE_ROOMS_URL_HERE"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

s = cloudscraper.create_scraper()
s.headers.update({"Accept-Language": "ru-RU,ru;q=0.9", "Referer": "https://www.cian.ru/"})

r = s.get(URL, timeout=30)
print(f"status={r.status_code} final_url={r.url} length={len(r.text)}")

states = _extract_initial_states(r.text)
print(f"decoded {len(states)} initialState blob(s)")

found = False
for i, st in enumerate(states):
    res = st.get("results")
    if isinstance(res, dict) and isinstance(res.get("offers"), list):
        offers = res["offers"]
        print(f"blob {i}: results.offers -> {len(offers)} offers")
        if offers:
            counts = dict(Counter(str(o.get("offerType")) for o in offers))
            total = res.get("aggregatedOffers") or res.get("extendedOffersCount")
            print(f"  offerType counts: {counts}   total reported: {total}")
            print(f"  sample: {offers[0].get('fullUrl', '?')}")
            found = True

if not found:
    with open("debug_probe.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    print("\nNo results.offers found -> page saved to debug_probe.html")
    print("Key map of blobs:")
    for i, st in enumerate(states):
        if not isinstance(st, dict):
            continue
        print(f"--- blob {i} ---")
        for k, v in st.items():
            if isinstance(v, dict):
                print(f"  {k}: dict -> {', '.join(list(v.keys())[:20])}")
            elif isinstance(v, list):
                print(f"  {k}: list ({len(v)})")
            else:
                print(f"  {k}: {type(v).__name__}")