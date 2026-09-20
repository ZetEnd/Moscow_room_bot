import cloudscraper

scraper = cloudscraper.create_scraper()
resp = scraper.get(
    "https://cian.ru/cat.php?engine_version=2&p=1&with_neighbors=0&region=1&deal_type=rent&offer_type=flat&type=4",
    timeout=15,
)

html = resp.text
with open("cian_page.html", "w", encoding="utf-8") as f:
    f.write(html)

print("Saved", len(html), "chars to cian_page.html")

for marker in ["__NEXT_DATA__", "_cianConfig", "offersSerialized", "initialState", "window.__INITIAL_STATE__"]:
    print(marker, "->", "FOUND" if marker in html else "not found")